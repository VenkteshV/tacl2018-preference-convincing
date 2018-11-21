"""
Scalable implementation of collaborative Gaussian process preference learning using stochastic variational inference.
Scales to large sets of observations (preference pairs) and numbers of items and users.

The results look different to the non-SVI version. There is a difference in how G is computed inside expec_t --
in the non-SVI version, it is computed separately for each observation location, and the obs_f estimates used to compute
it do not have any shared component across people because the value of t is computed by aggregating across people
outside the child GP. With the SVI implementation, the aggregation is done at each step by the inducing points, so that
inside the iterations of t_gp, there is a common t value when computing obs_f for all people. I think both are valid
approximations considering they are using slightly different values of obs_f to compute the updates. Differences may
accumulate from small differences in the approximations.

"""

import numpy as np
from scipy.stats import multivariate_normal as mvn, norm
import logging
from gp_pref_learning import GPPrefLearning, pref_likelihood
from scipy.linalg import block_diag
from scipy.special import psi, binom
from sklearn.cluster import MiniBatchKMeans

from collab_pref_learning_vb import CollabPrefLearningVB, expec_output_scale, expec_pdf_gaussian, expec_q_gaussian, \
    temper_extreme_probs, lnp_output_scale, lnq_output_scale


def svi_update_gaussian(invQi_y, mu0_n, mu_u, K_mm, invK_mm, K_nm, Lambda_factor1, K_nn, invQi, prev_invS, prev_invSm,
                        vb_iter, delay, forgetting_rate, N, update_size):
    Lambda_i = Lambda_factor1.dot(invQi).dot(Lambda_factor1.T)

    # calculate the learning rate for SVI
    rho_i = (vb_iter + delay) ** (-forgetting_rate)
    # print "\rho_i = %f " % rho_i

    # weighting. Lambda and
    w_i = N / float(update_size)

    # S is the variational covariance parameter for the inducing points, u.
    # Canonical parameter theta_2 = -0.5 * S^-1.
    # The variational update to theta_2 is (1-rho)*S^-1 + rho*Lambda. Since Lambda includes a sum of Lambda_i over
    # all data points i, the stochastic update weights a sample sum of Lambda_i over a mini-batch.
    invS = (1 - rho_i) * prev_invS + rho_i * (w_i * Lambda_i + invK_mm)

    # Variational update to theta_1 is (1-rho)*S^-1m + rho*beta*K_mm^-1.K_mn.y
    #     invSm = (1 - rho_i) * prev_invSm + w_i * rho_i * invK_mm.dot(K_im.T).dot(invQi).dot(y)
    invSm = (1 - rho_i) * prev_invSm + w_i * rho_i * Lambda_factor1.dot(invQi_y)

    # Next step is to use this to update f, so we can in turn update G. The contribution to Lambda_m and u_inv_S should therefore be made only once G has stabilised!
    # L_invS = cholesky(invS.T, lower=True, check_finite=False)
    # B = solve_triangular(L_invS, invK_mm.T, lower=True, check_finite=False)
    # A = solve_triangular(L_invS, B, lower=True, trans=True, check_finite=False, overwrite_b=True)
    # invK_mm_S = A.T
    S = np.linalg.inv(invS)
    invK_mm_S = invK_mm.dot(S)

    # fhat_u = solve_triangular(L_invS, invSm, lower=True, check_finite=False)
    # fhat_u = solve_triangular(L_invS, fhat_u, lower=True, trans=True, check_finite=False, overwrite_b=True)
    fhat_u = S.dot(invSm)
    fhat_u += mu_u

    # TODO: move the K_mm.T.dot(K_nm.T) computation out
    covpair_uS = K_nm.dot(invK_mm_S)
    fhat = covpair_uS.dot(invSm) + mu0_n
    if K_nn is None:
        C = None
    else:
        covpair = K_nm.dot(invK_mm)
        C = K_nn + (covpair_uS - covpair.dot(K_mm)).dot(covpair.T)
    return fhat, C, invS, invSm, fhat_u, invK_mm_S, S

def inducing_to_observation_moments(Ks_mm, invK_mm, K_nm, fhat_mm, mu0, S=None, K_nn=None, full_cov=True):
    covpair = K_nm.dot(invK_mm)
    fhat = covpair.dot(fhat_mm) + mu0

    if S is None:
        C = None
    elif full_cov:
        covpairS = covpair.dot(S)  # C_nm

        if K_nn is None:
            C = None
        else:
            C = K_nn + (covpairS - covpair.dot(Ks_mm)).dot(covpair.T)

    else:
        C = K_nn + np.sum(covpair.dot(S - Ks_mm) * covpair, axis=1)

    return fhat, C

class CollabPrefLearningSVI(CollabPrefLearningVB):

    def __init__(self, nitem_features, nperson_features=0, mu0=0, shape_s0=1, rate_s0=1,
                 shape_ls=1, rate_ls=100, ls=100, shape_lsy=1, rate_lsy=100, lsy=100, verbose=False, nfactors=20,
                 use_common_mean_t=True, kernel_func='matern_3_2',
                 max_update_size=500, ninducing=500, forgetting_rate=0.9, delay=1.0, use_lb=False):

        self.max_update_size = max_update_size
        self.ninducing_preset = ninducing
        self.forgetting_rate = forgetting_rate
        self.delay = delay

        self.conv_threshold_G = 1e-5

        self.t_mu0 = mu0

        super(CollabPrefLearningSVI, self).__init__(nitem_features, nperson_features, shape_s0, rate_s0,
                                                    shape_ls, rate_ls, ls, shape_lsy, rate_lsy, lsy, verbose, nfactors, use_common_mean_t,
                                                    kernel_func, use_lb=use_lb)

        if use_lb:
            self.n_converged = 10 # due to stochastic updates, take more iterations before assuming convergence

    def _init_covariance(self):
        self.shape_sw = np.zeros(self.Nfactors) + self.shape_sw0
        self.rate_sw = np.zeros(self.Nfactors) + self.rate_sw0

    def _choose_inducing_points(self):
        # choose a set of inducing points -- for testing we can set these to the same as the observation points.
        self.update_size = self.max_update_size # number of observed points in each stochastic update
        if self.update_size > self.nobs:
            self.update_size = self.nobs

        # Inducing points for items -----------------------------------------------------------

        self.ninducing = self.ninducing_preset

        if self.ninducing >= self.obs_coords.shape[0]:
            self.ninducing = self.obs_coords.shape[0]
            self.inducing_coords = self.obs_coords
        else:
            init_size = 300
            if init_size < self.ninducing:
                init_size = self.ninducing
            kmeans = MiniBatchKMeans(init_size=init_size, n_clusters=self.ninducing)
            kmeans.fit(self.obs_coords)

            self.inducing_coords = kmeans.cluster_centers_

        # Kernel over items (used to construct priors over w and t)
        if self.verbose:
            logging.debug('Initialising K_mm')
        self.K_mm = self.kernel_func(self.inducing_coords, self.ls) + 1e-6 * np.eye(self.ninducing) # jitter
        self.invK_mm = np.linalg.inv(self.K_mm)
        if self.verbose:
            logging.debug('Initialising K_nm')
        self.K_nm = self.kernel_func(self.obs_coords, self.ls, self.inducing_coords)

        # Related to w, the item components ------------------------------------------------------------
        # posterior expected values
        # self.w_u = mvn.rvs(np.zeros(self.ninducing), self.K_mm, self.Nfactors).reshape(self.Nfactors, self.ninducing)
        # self.w_u /= (self.shape_sw / self.rate_sw)[:, None]
        # self.w_u = self.w_u.T
        self.w_u = np.zeros((self.ninducing, self.Nfactors))
        # self.w_u[np.arange(self.ninducing), np.arange(self.ninducing)] = 1.0

        # moments of distributions over inducing points for convenience
        # posterior covariance
        self.winvS = np.array([self.invK_mm * self.shape_sw0 / self.rate_sw0 for _ in range(self.Nfactors)])
        self.winvSm = np.zeros((self.ninducing, self.Nfactors))

        # Inducing points for people -------------------------------------------------------------------
        if self.person_features is None:
            self.y_ninducing = self.Npeople

            # Prior covariance of y
            self.Ky_mm_block = np.ones(self.y_ninducing)
            self.invKy_mm_block = self.Ky_mm_block
            self.Ky_nm_block = np.diag(self.Ky_mm_block)

            # posterior covariance
            self.yS = np.array([self.Ky_mm_block for _ in range(self.Nfactors)])
            self.yinvS = np.array([self.invKy_mm_block for _ in range(self.Nfactors)])
            self.yinvSm = np.zeros((self.y_ninducing, self.Nfactors))

            if self.y_ninducing <= self.Nfactors:
                # give each person a factor of their own, with a little random noise so that identical users will
                # eventually get clustered into the same set of factors.
                self.y_u = np.zeros((self.Nfactors, self.y_ninducing))
                self.y_u[:self.y_ninducing, :] = np.eye(self.y_ninducing)
                self.y_u += np.random.rand(*self.y_u.shape) * 1e-6
            else:
                self.y_u = norm.rvs(0, 1, (self.Nfactors, self.y_ninducing))

        else:
            self.y_ninducing = self.ninducing_preset

            if self.y_ninducing >= self.Npeople:
                self.y_ninducing = self.Npeople
                self.y_inducing_coords = self.person_features
            else:
                init_size = 300
                if self.y_ninducing > init_size:
                    init_size = self.y_ninducing
                kmeans = MiniBatchKMeans(init_size=init_size, n_clusters=self.y_ninducing)
                kmeans.fit(self.person_features)

                self.y_inducing_coords = kmeans.cluster_centers_

            # Kernel over people used to construct prior covariance for y
            if self.verbose:
                logging.debug('Initialising Ky_mm')
            self.Ky_mm_block = self.y_kernel_func(self.y_inducing_coords, self.lsy)
            self.Ky_mm_block += 1e-6 * np.eye(len(self.Ky_mm_block)) # jitter

            # Prior covariance of y
            self.invKy_mm_block = np.linalg.inv(self.Ky_mm_block)

            if self.verbose:
                logging.debug('Initialising Ky_nm')
            self.Ky_nm_block = self.y_kernel_func(self.person_features, self.lsy, self.y_inducing_coords)

            # posterior covariance
            self.yS = np.array([self.Ky_mm_block for _ in range(self.Nfactors)])
            self.yinvS = np.array([self.invKy_mm_block for _ in range(self.Nfactors)])
            self.yinvSm = np.zeros((self.y_ninducing, self.Nfactors))

            # posterior means
            if self.y_ninducing <= self.Nfactors:
                # give each person a factor of their own, with a little random noise so that identical users will
                # eventually get clustered into the same set of factors.
                self.y_u = np.zeros((self.Nfactors, self.y_ninducing))
                self.y_u[:self.y_ninducing, :] = np.eye(self.y_ninducing)
                self.y_u += np.random.rand(*self.y_u.shape) * 1e-6
            else:
                self.y_u = mvn.rvs(np.zeros(self.y_ninducing), self.Ky_mm_block, self.Nfactors)

        if self.Nfactors == 1:
            self.y_u = self.y_u[None, :]

        # Related to t, the item means -----------------------------------------------------------------
        self.t_u = np.zeros((self.ninducing, 1))  # posterior means
        self.tS = None

        if self.use_t:
            self.tinvSm = np.zeros((self.ninducing, 1), dtype=float)# theta_1/posterior covariance dot means
            self.tinvS = self.invK_mm * self.shape_st0 / self.rate_st0 # theta_2/posterior covariance


    def _post_sample(self, K_nm, invK_mm, w_u, wS, t_u, tS,
                     Ky_nm, invKy_mm, y_u, y_var, v, u, expectedlog=False):

        # sample the inducing points because we don't have full covariance matrix. In this case, f_cov should be Ks_nm
        nsamples = 500

        if wS.ndim == 3:
            w_samples = np.array([mvn.rvs(mean=w_u[:, f], cov=wS[f], size=(nsamples))
                              for f in range(self.Nfactors)])

            # w_samples = np.array([np.random.normal(loc=w_u[:, f:f+1], scale=np.sqrt(np.diag(wS[f]))[:, None],
            #                       size=(w_u.shape[0], nsamples)).T
            #                       for f in range(self.Nfactors)])
        else:
            w_samples = np.array([mvn.rvs(mean=w_u[:, f], cov=wS, size=(nsamples))
                                  for f in range(self.Nfactors)])

            # w_samples = np.array([np.random.normal(loc=w_u[:, f:f+1], scale=np.sqrt(np.diag(wS))[:, None],
            #                       size=(w_u.shape[0], nsamples)).T
            #                       for f in range(self.Nfactors)])

        if self.use_t:
            if np.isscalar(t_u):
                t_u = np.zeros(tS.shape[0]) + t_u
            else:
                t_u = t_u.flatten()

            t_samples = mvn.rvs(mean=t_u, cov=tS, size=(nsamples))
            # cheat for the speedup. It increases the noise so the results can be overly conservative.
            # t_samples = np.random.normal(loc=t_u[:, None], scale=np.sqrt(np.diag(tS))[:, None],
            #                                                              size=(t_u.shape[0], nsamples)).T

        N = y_u.shape[1]
        if np.isscalar(y_var):
            y_var = np.zeros((self.Nfactors * N)) + y_var
        else:
            y_var = y_var.flatten()

        y_samples = np.random.normal(loc=y_u.flatten()[:, None], scale=np.sqrt(y_var)[:, None],
                                     size=(N * self.Nfactors, nsamples)).reshape(self.Nfactors, N, nsamples)

        # w_samples: F x nsamples x N
        # t_samples: nsamples x N
        # y_samples: F x Npeople x nsamples

        if K_nm is not None:
            covpair_w = K_nm.dot(invK_mm)
            w_samples = np.array([covpair_w.dot(w_samples[f].T).T for f in range(self.Nfactors)])  # assume zero mean
            if self.use_t:
                t_samples = K_nm.dot(invK_mm).dot(t_samples.T).T  # assume zero mean

            if self.person_features is not None:
                covpair_y = Ky_nm.dot(invKy_mm)
                y_samples = np.array([covpair_y.dot(y_samples[f]) for f in range(self.Nfactors)])  # assume zero mean

        if self.use_t:
            f_samples = np.array([w_samples[:, s, :].T.dot(y_samples[:, :, s]).T + t_samples[s][None, :]for s in range(nsamples)])
        else:
            f_samples = np.array([w_samples[:, s, :].T.dot(y_samples[:, :, s]).T for s in range(nsamples)])

        f_samples = f_samples.reshape(nsamples, self.N * self.Npeople).T

        phi = pref_likelihood(f_samples, v=v, u=u)
        phi = temper_extreme_probs(phi)
        notphi = 1 - phi

        if expectedlog:
            phi = np.log(phi)
            notphi = np.log(notphi)

        m_post = np.mean(phi, axis=1)[:, np.newaxis]
        not_m_post = np.mean(notphi, axis=1)[:, np.newaxis]
        v_post = np.var(phi, axis=1)[:, np.newaxis]
        v_post = temper_extreme_probs(v_post, zero_only=True)
        # fix extreme values to sensible values. Don't think this is needed and can lower variance?
        v_post[m_post * (1 - not_m_post) <= 1e-7] = 1e-8

        return m_post, not_m_post, v_post

    def _estimate_obs_noise(self):

        # to make a and b smaller and put more weight onto the observations, increase v_prior by increasing rate_s0/shape_s0
        m_prior, not_m_prior, v_prior = self._post_sample(self.K_nm, self.invK_mm,
                np.zeros((self.ninducing, self.Nfactors)), self.K_mm * self.rate_sw0 / self.shape_sw0,
                self.t_mu0, self.K_mm * self.rate_st0 / self.shape_st0,
                self.Ky_nm_block, self.invKy_mm_block, np.zeros((self.Nfactors, self.y_ninducing)), 1,
                self.pref_v, self.pref_u)

        # find the beta parameters
        a_plus_b = 1.0 / (v_prior / (m_prior*not_m_prior)) - 1
        a = (a_plus_b * m_prior)
        b = (a_plus_b * not_m_prior)

        nu0 = np.array([b, a])
        # Noise in observations
        nu0_total = np.sum(nu0, axis=0)
        obs_mean = (self.z + nu0[1]) / (1 + nu0_total)
        var_obs_mean = obs_mean * (1 - obs_mean) / (1 + nu0_total + 1)  # uncertainty in obs_mean
        Q = (obs_mean * (1 - obs_mean) + var_obs_mean)
        Q = Q.flatten()
        return Q

    def _init_w(self):
        # initialise the factors randomly -- otherwise they can get stuck because there is nothing to differentiate them
        # i.e. the cluster identifiability problem
        # self.w = np.zeros((self.N, self.Nfactors))
        self.w = self.K_nm.dot(self.invK_mm).dot(self.w_u)

        self.Sigma_w = np.zeros((self.Nfactors, self.ninducing, self.ninducing))

        if not self.new_obs:
            return

        self.Q = self._estimate_obs_noise()

    def _init_t(self):
        self.t = np.zeros((self.N, 1))
        self.st = self.shape_st0 / self.rate_st0

        if not self.use_t:
            return

    def _init_y(self):
        if self.person_features is None:
            self.y = self.y_u
        else:
            self.y = self.Ky_nm_block.dot(self.invKy_mm_block).dot(self.y_u.T).T
        self.y_var = np.ones((self.Nfactors, self.Npeople))

        if self.person_features is None:
            self.Sigma_y = np.zeros((self.Nfactors, self.y_ninducing))
        else:
            self.Sigma_y = np.zeros((self.Nfactors, self.y_ninducing, self.y_ninducing))

    def _init_params(self):
        if self.Nfactors is None or self.Npeople < self.Nfactors:  # not enough items or people
            self.Nfactors = self.Npeople

        self._init_covariance()

        # initialise the inducing points first
        self._choose_inducing_points()

        self.ls = np.zeros(self.nitem_features) + self.ls

        self.G = -np.inf

        self._init_w()
        self._init_y()
        self._init_t()


    def _compute_jacobian(self):
        self.obs_f = (self.w.dot(self.y) + self.t).T.reshape(self.N * self.Npeople, 1)

        phi, g_mean_f = pref_likelihood(self.obs_f, v=self.pref_v, u=self.pref_u, return_g_f=True)  # first order Taylor series approximation
        J = 1 / (2 * np.pi) ** 0.5 * np.exp(-g_mean_f ** 2 / 2.0) * np.sqrt(0.5)
        J = J[self.data_obs_idx_i, :]

        s = (self.pref_v[self.data_obs_idx_i, None] == self.joint_idx_i[None, :]).astype(int) - \
            (self.pref_u[self.data_obs_idx_i, None] == self.joint_idx_i[None, :]).astype(int)

        J_df = J * s

        return J_df


    def _expec_t(self):

        self._update_sample()

        if not self.use_t:
            return

        N = self.ninducing

        # self.t_gp.s = self.st
        # self.t_gp.fit(self.pref_v, self.pref_u, self.dummy_obs_coords, self.preferences,
        #               mu0=mu0, K=None, process_obs=self.new_obs, input_type=self.input_type)

        rho_i = (self.vb_iter + self.delay) ** (-self.forgetting_rate)
        w_i = self.nobs / float(self.update_size)

        self.prev_tinvS = self.tinvS
        self.prev_tinvSm = self.tinvSm

        covpair = self.invK_mm.dot(self.K_nm[self.w_idx_i].T)

        for G_iter in range(self.max_iter_G):

            oldG = self.G
            self.G = self._compute_jacobian() # P_i x (N_i*Npeople_i)

            # we need to map from the real Npeople points to the inducing points.
            GinvQG = (self.G.T/self.Q[None, self.data_obs_idx_i]).dot(self.G)

            self.Sigma_t = covpair.dot(GinvQG).dot(covpair.T)

            # need to get invS for current iteration and merge using SVI weighted sum
            self.tinvS = (1-rho_i) * self.prev_tinvS + rho_i * (self.invK_mm*self.shape_st/self.rate_st + w_i * self.Sigma_t)

            z0 = pref_likelihood(self.obs_f, v=self.pref_v[self.data_obs_idx_i], u=self.pref_u[self.data_obs_idx_i]) \
                 - self.G.dot(self.t[self.w_idx_i, :]) # P x NU_i

            invQ_f = (self.G.T / self.Q[None, self.data_obs_idx_i]).dot(self.z[self.data_obs_idx_i] - z0)
            x = covpair.dot(invQ_f)

            # need to get x for current iteration and merge using SVI weighted sum
            self.tinvSm = (1-rho_i) * self.prev_tinvSm + rho_i * w_i * x

            self.tS = np.linalg.inv(self.tinvS)
            self.t_u = self.tS.dot(self.tinvSm)

            diff = np.max(np.abs(oldG - self.G))

            if self.verbose:
                logging.debug("expec_t: iter %i, G-diff=%f" % (G_iter, diff))

            if diff < self.conv_threshold_G:
                break

        self.t, _ = inducing_to_observation_moments(self.Kts_mm, self.invK_mm, self.K_nm, self.t_u, self.t_mu0)

        self.shape_st, self.rate_st = expec_output_scale(self.shape_st0, self.rate_st0, N,
                                                         self.invK_mm, self.t_u, np.zeros((N, 1)),
                                                         f_cov=self.tS)
        self.st = self.shape_st / self.rate_st


    def _expec_w(self):
        """
        Compute the expectation over the latent features of the items and the latent personality components
        """
        # Put a GP prior on w with covariance K/gamma and mean 0
        N = self.ninducing

        rho_i = (self.vb_iter + self.delay) ** (-self.forgetting_rate)
        w_i = self.nobs / float(self.update_size)

        self.prev_winvS = self.winvS
        self.prev_winvSm = self.winvSm

        covpair = self.invK_mm.dot(self.K_nm[self.w_idx_i].T)

        for G_iter in range(self.max_iter_G):

            oldG = self.G
            self.G = self._compute_jacobian() # P_i x (N_i*Npeople_i)

            # we need to map from the real Npeople points to the inducing points.
            # invQ = (self.G.T / self.Q[None, self.data_obs_idx_i]).dot(self.G)
            invQG = self.G.T/self.Q[None, self.data_obs_idx_i]

            scaling = np.zeros((self.Nfactors, self.update_size))

            for f in range(self.Nfactors):
                # scale the precision by y
                scaling_f = self.y[f:f+1, self.personIDs[self.data_obs_idx_i]]**2 + \
                              self.y_var[f:f+1, self.personIDs[self.data_obs_idx_i]]

                scaling[f] = scaling_f

                invQ_scaled =  (scaling_f * invQG).dot(self.G)

                Sigma_f = covpair.dot(invQ_scaled).dot(covpair.T)

                self.Sigma_w[f, :, :] = Sigma_f

            # need to get invS for current iteration and merge using SVI weighted sum
            self.winvS = (1-rho_i) * self.prev_winvS + rho_i * (self.invK_mm[None, :, :]*self.shape_sw[:, None, None]
                            /self.rate_sw[:, None, None] + w_i * self.Sigma_w)

            z0 = pref_likelihood(self.obs_f, v=self.pref_v[self.data_obs_idx_i], u=self.pref_u[self.data_obs_idx_i]) \
                 - self.G.dot(self.w[self.w_idx_i, :] * self.y[:, self.y_idx_i].T) # P x NU_i

            invQ_f = (self.G.T / self.Q[None, self.data_obs_idx_i]).dot(self.y[:, self.personIDs[self.data_obs_idx_i]].T
                                                                   * (self.z[self.data_obs_idx_i] - z0))  # Npoints_in_sample x Nfactors
            x = covpair.dot(invQ_f)

            # need to get x for current iteration and merge using SVI weighted sum
            self.winvSm = (1-rho_i) * self.prev_winvSm + rho_i * w_i * x

            self.wS = np.array([np.linalg.inv(self.winvS[f]) for f in range(self.Nfactors)])
            self.w_u = np.array([self.wS[f].dot(self.winvSm[:, f]) for f in range(self.Nfactors)]).T

            for f in range(self.Nfactors):
                self.w[:, f:f+1], _ = inducing_to_observation_moments(self.K_mm / self.shape_sw[f] * self.rate_sw[f],
                                    self.invK_mm, self.K_nm, self.w_u[:, f:f+1], 0)

            diff = np.max(np.abs(oldG - self.G))

            if self.verbose:
                logging.debug("expec_w: iter %i, G-diff=%f" % (G_iter, diff))

            if diff < self.conv_threshold_G:
                break

        if self.verbose:
            logging.debug('Computing Kw_i')
        Kw_i = self.kernel_func(self.obs_coords[self.n_idx_i, :], self.ls)
        self.w_cov_i = []
        self.w_i = []

        for f in range(self.Nfactors):
            Kw_i_f = Kw_i / self.shape_sw[f] * self.rate_sw[f]
            w_i_f, w_cov_i_f = inducing_to_observation_moments(self.K_mm / self.shape_sw[f] * self.rate_sw[f],
                self.invK_mm, self.K_nm[self.n_idx_i], self.w_u[:, f:f+1], 0, self.wS[f], Kw_i_f, full_cov=True)

            self.w_i.append(w_i_f)
            self.w_cov_i.append(w_cov_i_f)

        self.w_i = np.array(self.w_i)

        for f in range(self.Nfactors):
            self.shape_sw[f], self.rate_sw[f] = expec_output_scale(self.shape_sw0, self.rate_sw0, N,
                                                       self.invK_mm, self.w_u[:, f:f + 1], np.zeros((N, 1)),
                                                       f_cov=self.wS[f])


    def _expec_y(self):
        rho_i = (self.vb_iter + self.delay) ** (-self.forgetting_rate)
        w_i = np.sum(self.nobs) / float(self.update_size)

        self.prev_yinvSm = self.yinvSm
        self.prev_yinvS = self.yinvS

        if self.person_features is not None:
            covpair = self.invKy_mm_block.dot(self.Ky_nm_block[self.y_idx_i].T)
        else:
            covpair = self.Ky_nm_block[self.y_idx_i, :].T

        for G_iter in range(self.max_iter_G):
            oldG = self.G

            self.G = self._compute_jacobian()

            invQG =  self.G / self.Q[self.data_obs_idx_i, None]

            scaling = np.zeros((self.update_size, self.Nfactors))

            for f in range(self.Nfactors):
                # scale the precision by w
                scaling_f = self.w[self.tpref_v[self.data_obs_idx_i], f]**2 \
                  + self.w[self.tpref_u[self.data_obs_idx_i], f]**2 \
                  - 2 * self.w[self.tpref_v[self.data_obs_idx_i], f] * self.w[self.tpref_u[self.data_obs_idx_i], f] \
                  + self.w_cov_i[f][self.pref_u_w_idx, self.pref_u_w_idx] \
                  + self.w_cov_i[f][self.pref_v_w_idx, self.pref_v_w_idx] \
                  - 2 * self.w_cov_i[f][self.pref_v_w_idx, self.pref_u_w_idx]

                scaling[:, f] = scaling_f

                if self.person_features is None:
                    self.Sigma_y[f, :] = covpair.dot(np.sum(scaling_f[:, None] * invQG, axis=0)[:, None]).flatten()
                else:
                    invQ_scaled = self.G.T.dot(scaling_f[:, None] * invQG)
                    self.Sigma_y[f, :, :] = covpair.dot(invQ_scaled).dot(covpair.T)

            # need to get invS for current iteration and merge using SVI weighted sum
            if self.person_features is not None:
                self.yinvS = (1-rho_i) * self.prev_yinvS + rho_i * (
                        self.invKy_mm_block[None, :, :] + w_i * self.Sigma_y)
            else:
                self.yinvS = (1 - rho_i) * self.prev_yinvS + rho_i * (1 + w_i * self.Sigma_y)

            z0 = pref_likelihood(self.obs_f, v=self.pref_v[self.data_obs_idx_i], u=self.pref_u[self.data_obs_idx_i]) \
                 - self.G.dot(self.w[self.w_idx_i, :] * self.y[:, self.y_idx_i].T)

            invQ_f = (self.G.T / self.Q[None, self.data_obs_idx_i]).dot((self.w[self.tpref_v[self.data_obs_idx_i]]
                            - self.w[self.tpref_u[self.data_obs_idx_i]]) * (self.z[self.data_obs_idx_i] - z0))

            x = covpair.dot(invQ_f)

            # need to get x for current iteration and merge using SVI weighted sum
            self.yinvSm = (1-rho_i) * self.prev_yinvSm + rho_i * w_i * x

            if self.person_features is None:
                self.yS = 1.0 / self.yinvS
                self.y_u = (self.yS.T * self.yinvSm).T
                self.y = self.y_u
                self.y_var = self.yS
            else:
                self.yS = np.array([np.linalg.inv(self.yinvS[f]) for f in range(self.Nfactors)])
                self.y_u = np.array([self.yS[f].dot(self.yinvSm)[:, f] for f in range(self.Nfactors)])
                for f in range(self.Nfactors):
                    yf, varyf = inducing_to_observation_moments(self.Ky_mm_block,
                            self.invKy_mm_block, self.Ky_nm_block, self.y_u[f:f+1, :].T, 0, self.yS[f], 1, full_cov=False)
                    self.y[f:f + 1] = yf.T
                    self.y_var[f:f + 1] = varyf.T

            diff = np.max(np.abs(oldG - self.G))

            if self.verbose:
                logging.debug("expec_y: iter %i, G-diff=%f" % (G_iter, diff))

            if diff < self.conv_threshold_G:
                break


    def _update_sample_idxs(self):

        self.data_obs_idx_i = np.sort(np.random.choice(self.nobs, self.update_size, replace=False))

        data_idx_i = np.zeros((self.N, self.Npeople), dtype=bool)
        data_idx_i[self.tpref_v[self.data_obs_idx_i], self.personIDs[self.data_obs_idx_i]] = True
        data_idx_i[self.tpref_u[self.data_obs_idx_i], self.personIDs[self.data_obs_idx_i]] = True

        separate_idx_i = np.argwhere(data_idx_i.T)
        self.y_idx_i = separate_idx_i[:, 0]
        self.w_idx_i = separate_idx_i[:, 1]
        self.joint_idx_i = self.w_idx_i + (self.N * self.y_idx_i)

        self.n_idx_i, pref_idxs = np.unique([self.tpref_v[self.data_obs_idx_i], self.tpref_u[self.data_obs_idx_i]],
                                            return_inverse=True)
        pref_idxs = pref_idxs.reshape(2, self.update_size)

        # the index into n_idx_i for each of the selected prefs
        self.pref_v_w_idx = pref_idxs[0]#np.array([np.argwhere(self.n_idx_i == n).flatten() for n in self.tpref_v[self.data_obs_idx_i]])
        self.pref_u_w_idx = pref_idxs[1]#np.array([np.argwhere(self.n_idx_i == n).flatten() for n in self.tpref_u[self.data_obs_idx_i]])

    def _update_sample(self):

        self._update_sample_idxs()

        if self.use_t:
            self.Kts_mm = self.K_mm / self.st

        self.G = -np.inf # need to reset G because we have a new sample to compute it for

    def data_ll(self, logrho, lognotrho):
        bc = binom(np.ones(self.z.shape), self.z)
        logbc = np.log(bc)
        lpobs = np.sum(self.z * logrho + (1 - self.z) * lognotrho)
        lpobs += np.sum(logbc)

        data_ll = lpobs
        return data_ll

    def _logpD(self):
        # K_star, um_minus_mu0, uS, invK_mm, v, u
        if self.person_features is None:
            y_var = self.yS
        else:
            y_var = np.array([np.diag(self.yS[f]) for f in range(self.Nfactors)])
        logrho, lognotrho, _ = self._post_sample(self.K_nm, self.invK_mm, self.w_u, self.wS, self.t_u, self.tS,
                                     self.Ky_nm_block, self.invKy_mm_block, self.y_u,
                                     y_var,
                                     self.pref_v, self.pref_u, expectedlog=True)


        data_ll = self.data_ll(logrho, lognotrho)

        return data_ll

    def lowerbound(self):

        data_ll = self._logpD()

        Elnsw = psi(self.shape_sw) - np.log(self.rate_sw)
        if self.use_t:
            Elnst = psi(self.shape_st) - np.log(self.rate_st)
            st = self.st
        else:
            Elnst = 0
            st = 1

        sw = self.shape_sw / self.rate_sw

        # the parameter N is not multiplied here by Nfactors because it will be multiplied by the s value for each
        # factor and summed inside the function
        logpw = np.sum([expec_pdf_gaussian(self.K_mm, self.invK_mm, Elnsw[f], self.ninducing,
                    self.shape_sw[f] / self.rate_sw[f], self.w_u[:, f:f+1], 0, self.wS[f], 0)
                        for f in range(self.Nfactors)])

        logqw = np.sum([expec_q_gaussian(self.wS[f], self.ninducing * self.Nfactors) for f in range(self.Nfactors)])

        if self.use_t:
            logpt = expec_pdf_gaussian(self.K_mm, self.invK_mm, Elnst, self.ninducing, st, self.t_u, self.t_mu0,
                                       0, 0) - 0.5 * self.ninducing
            logqt = expec_q_gaussian(self.tS, self.ninducing)
        else:
            logpt = 0
            logqt = 0

        logpy = np.sum([expec_pdf_gaussian(self.Ky_mm_block, self.invKy_mm_block, 0, self.y_ninducing, 1,
                                   self.y_u[f:f+1, :].T, 0, self.yS[f], 0) for f in range(self.Nfactors)])
        logqy = np.sum([expec_q_gaussian(self.yS[f], self.y_ninducing * self.Nfactors) for f in range(self.Nfactors)])

        # if self.nperson_features is not None:
        # else:
            # logpy = 0
            # for f in range(self.Nfactors):
                # logpy += np.sum(norm.logpdf(self.y[f, :], scale=1))
            # logqy = mvn.logpdf(self.y.flatten(), mean=self.y.flatten(), cov=self.y_cov)

        logps_w = 0
        logqs_w = 0
        for f in range(self.Nfactors):
            logps_w += lnp_output_scale(self.shape_sw0, self.rate_sw0, self.shape_sw[f], self.rate_sw[f], sw[f],
                                        Elnsw[f])
            logqs_w += lnq_output_scale(self.shape_sw[f], self.rate_sw[f], sw[f], Elnsw[f])

        logps_t = lnp_output_scale(self.shape_st0, self.rate_st0, self.shape_st, self.rate_st, st, Elnst)
        logqs_t = lnq_output_scale(self.shape_st, self.rate_st, st, Elnst)

        w_terms = logpw - logqw + logps_w - logqs_w
        y_terms = logpy - logqy
        t_terms = logpt - logqt + logps_t - logqs_t

        lb = data_ll + t_terms + w_terms + y_terms

        if self.verbose:
            logging.debug('s_w=%s' % (self.shape_sw / self.rate_sw))
            logging.debug('s_t=%.2f' % (self.shape_st / self.rate_st))

        if self.verbose:
            logging.debug('likelihood=%.3f, wterms=%.3f, yterms=%.3f, tterms=%.3f' % (data_ll, w_terms, y_terms, t_terms))

        logging.debug("Iteration %i: Lower bound = %.3f, " % (self.vb_iter, lb))

        if self.verbose:
            logging.debug("t: %.2f, %.2f" % (np.min(self.t), np.max(self.t)))
            logging.debug("w: %.2f, %.2f" % (np.min(self.w), np.max(self.w)))
            logging.debug("y: %f, %f" % (np.min(self.y), np.max(self.y)))

        return lb

    def _predict_w_t(self, coords_1, return_cov=True):

        # kernel between pidxs and t
        if self.verbose:
            logging.debug('Computing K_nm in predict_w_t')
        K = self.kernel_func(coords_1, self.ls, self.inducing_coords)
        if self.verbose:
            logging.debug('Computing K_nn in predict_w_t')
        K_starstar = self.kernel_func(coords_1, self.ls, coords_1)
        covpair = K.dot(self.invK_mm)
        N = coords_1.shape[0]

        # use kernel to compute t.
        if self.use_t:
            t_out = K.dot(self.invK_mm).dot(self.t_u)

            covpair_uS = covpair.dot(self.tS)
            if return_cov:
                cov_t = K_starstar * self.rate_st / self.shape_st + (covpair_uS -
                                                                     covpair.dot(self.Kts_mm)).dot(covpair.T)
            else:
                cov_t = None
        else:
            t_out = np.zeros((N, 1))
            if return_cov:
                cov_t = np.zeros((N, N))
            else:
                cov_t = None

        # kernel between pidxs and w -- use kernel to compute w. Don't need Kw_mm block-diagonal matrix
        w_out = K.dot(self.invK_mm).dot(self.w_u)

        if return_cov:
            cov_w = np.zeros((self.Nfactors, N, N))
            for f in range(self.Nfactors):
                cov_w[f] = K_starstar  * self.rate_sw[f] / self.shape_sw[f] + \
                                covpair.dot(self.wS[f] - self.K_mm * self.rate_sw[f] / self.shape_sw[f]).dot(covpair.T)
        else:
            cov_w = None

        return t_out, w_out, cov_t, cov_w

    def predict_t(self, item_features):
        '''
        Predict the common consensus function values using t
        '''
        if item_features is None:
            # reuse the training points
            t = self.t
        else:
            # kernel between pidxs and t
            if self.verbose:
                logging.debug('Computing K_nm in predict_t')
            K = self.kernel_func(item_features, self.ls, self.inducing_coords)
            N = item_features.shape[0]

            # use kernel to compute t.
            if self.use_t:
                t = K.dot(self.invK_mm).dot(self.t_u)
            else:
                t = np.zeros((N, 1))

        return t

    def predict_common(self, item_features, item_0_idxs, item_1_idxs):
        '''
        Predict the common consensus pairwise labels using t.
        '''
        if not self.use_t:
            return np.zeros(len(item_0_idxs))

        if self.verbose:
            logging.debug('Computing K_nm in predict_common')
        K = self.kernel_func(item_features, self.ls, self.inducing_coords)
        if self.verbose:
            logging.debug('Computing K_nn in predict_common')
        K_starstar = self.kernel_func(item_features, self.ls, item_features)
        covpair = K.dot(self.invK_mm)
        covpair_uS = covpair.dot(self.tS)

        t_out = K.dot(self.invK_mm).dot(self.t_u)
        cov_t = K_starstar * self.rate_st / self.shape_st + (covpair_uS - covpair.dot(self.Kts_mm)).dot(covpair.T)

        predicted_prefs = pref_likelihood(t_out, cov_t[item_0_idxs, item_1_idxs]
                                          + cov_t[item_0_idxs, item_1_idxs]
                                          - cov_t[item_0_idxs, item_1_idxs]
                                          - cov_t[item_0_idxs, item_1_idxs],
                                          subset_idxs=[], v=item_0_idxs, u=item_1_idxs)

        return predicted_prefs

    def _y_var(self):
        if self.person_features is None:
            return self.yS

        v = np.array([inducing_to_observation_moments(self.Ky_mm_block, self.invKy_mm_block, self.Ky_nm_block,
                                       self.y_u[f:f+1, :].T, 0, S=self.yS[f], K_nn=1.0, full_cov=False)[1]
                      for f in range(self.Nfactors)])
        return v

    def _predict_y(self, person_features, return_cov=True):

        if person_features is None and self.person_features is None:

            if return_cov:
                cov_y = np.zeros((self.Nfactors, self.y_ninducing, self.y_ninducing))
                for f in range(self.Nfactors):
                    cov_y[f] = self.yS[f]
            else:
                cov_y = None

            return self.y_u, cov_y

        elif person_features is None:
            person_features = self.person_features
            Ky = self.Ky_nm_block
            Ky_starstar = self.Ky_nm_block.dot(self.invKy_mm_block).dot(self.Ky_nm_block.T)

        else:
            if self.verbose:
                logging.debug('Computing Ky_nm in predict_y')
            Ky = self.y_kernel_func(person_features, self.lsy, self.y_inducing_coords)
            if self.verbose:
                logging.debug('Computing Ky_nn in predict_y')
            Ky_starstar = self.y_kernel_func(person_features, self.lsy, person_features)

        covpair = Ky.dot(self.invKy_mm_block)
        Npeople = person_features.shape[0]

        y_out = Ky.dot(self.invKy_mm_block).dot(self.y_u.T).T

        if return_cov:
            cov_y = np.zeros((self.Nfactors, Npeople, Npeople))
            for f in range(self.Nfactors):
                cov_y[f] = Ky_starstar + covpair.dot(self.yS[f] - self.Ky_mm_block).dot(covpair.T)
        else:
            cov_y = None

        return y_out, cov_y

    def _gradient_dim(self, lstype, d, dimension):
        der_logpw_logqw = 0
        der_logpy_logqy = 0
        der_logpt_logqt = 0

        # compute the gradient. This should follow the MAP estimate from chu and ghahramani.
        # Terms that don't involve the hyperparameter are zero; implicit dependencies drop out if we only calculate
        # gradient when converged due to the coordinate ascent method.
        if lstype == 'item' or (lstype == 'both' and d < self.nitem_features):
            dKdls = self.K_mm * self.kernel_der(self.inducing_coords, self.ls, dimension)
            # try to make the s scale cancel as much as possible
            invK_w = self.invK_mm.dot(self.w_u)

            for f in range(self.Nfactors):
                swf = self.shape_sw[f] / self.rate_sw[f]
                invKs_Cf = (self.invK_mm * self.shape_sw[f] / self.rate_sw[f]).dot(self.wS[f])
                invK_wf = invK_w[:, f]

                Sigma = self.Sigma_w[f, :, :]

                der_logpw_logqw += 0.5 * (invK_wf.T.dot(dKdls).dot(invK_wf) * swf -
                                    np.trace(invKs_Cf.dot(Sigma).dot(dKdls / swf)))

            if self.use_t:
                invK_t = self.invK_mm.dot(self.t_u)
                invKs_C = self.invK_mm.dot(self.tS) * self.st

                der_logpt_logqt = 0.5 * (invK_t.T.dot(dKdls).dot(invK_t) * self.st -
                            np.trace(invKs_C.dot(self.Sigma_t).dot(dKdls / self.st)))

        elif (lstype == 'person' or (lstype == 'both' and d >= self.nitem_features)) and self.person_features is not None:
            dKdls = self.Ky_mm_block * self.kernel_der(self.y_inducing_coords, self.lsy, dimension)
            invK_y = self.invKy_mm_block.dot(self.y_u.T)

            for f in range(self.Nfactors):
                invKs_Cf = self.invKy_mm_block.dot(self.yS[f])
                invK_yf = invK_y[:, f]

                Sigma = self.Sigma_y[f, :, :]

                der_logpy_logqy += 0.5 * (invK_yf.T.dot(dKdls).dot(invK_yf) - np.trace(invKs_Cf.dot(Sigma).dot(dKdls)))

        return der_logpw_logqw + der_logpy_logqy + der_logpt_logqt