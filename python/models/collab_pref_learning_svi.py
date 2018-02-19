"""
Scalable implementation of collaborative Gaussian process preference learning using stochastic variational inference.
Scales to large sets of observations (preference pairs) and numbers of items and users.
"""

import numpy as np
from scipy.stats import multivariate_normal as mvn
import logging
from gp_pref_learning import GPPrefLearning, pref_likelihood
from scipy.linalg import block_diag
from scipy.special import psi
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

def inducing_to_observation_moments(Ks_mm, invK_mm, K_nm, fhat_mm, mu0, S=None, K_nn=None):
    covpair = K_nm.dot(invK_mm)
    fhat = covpair.dot(fhat_mm) + mu0

    if S is None:
        covpairS = None
        C = None
    else:
        covpairS = covpair.dot(S) # C_nm

        if K_nn is None:
            C = None
        else:
            C = K_nn + (covpairS - covpair.dot(Ks_mm)).dot(covpair.T)

    return fhat, C, covpairS

class CollabPrefLearningSVI(CollabPrefLearningVB):

    def __init__(self, nitem_features, nperson_features=0, shape_s0=1, rate_s0=1,
                 shape_ls=1, rate_ls=100, ls=100, shape_lsy=1, rate_lsy=100, lsy=100, verbose=False, nfactors=20,
                 use_common_mean_t=True, kernel_func='matern_3_2',
                 max_update_size=10000, ninducing=500, forgetting_rate=0.9, delay=1.0):

        self.max_update_size = max_update_size
        self.ninducing_preset = ninducing
        self.forgetting_rate = forgetting_rate
        self.delay = delay

        self.conv_threshold_G = 1e-5

        self.t_mu0_u = 0

        super(CollabPrefLearningSVI, self).__init__(nitem_features, nperson_features, shape_s0, rate_s0,
                                                    shape_ls, rate_ls, ls, shape_lsy, rate_lsy, lsy, verbose, nfactors, use_common_mean_t,
                                                    kernel_func)

        self.n_converged = 10 # due to stochastic updates, take more iterations before assuming convergence

    def _init_covariance(self):
        self.shape_sw = np.zeros(self.Nfactors) + self.shape_sw0
        self.rate_sw = np.zeros(self.Nfactors) + self.rate_sw0
        self.shape_sy = np.zeros(self.Nfactors) + self.shape_sy0
        self.rate_sy = np.zeros(self.Nfactors) + self.rate_sy0

    def _choose_inducing_points(self):
        # choose a set of inducing points -- for testing we can set these to the same as the observation points.
        nobs = len(self.preferences)

        self.update_size = self.max_update_size # number of observed points in each stochastic update
        if self.update_size > nobs:
            self.update_size = nobs

        # Inducing points for items -----------------------------------------------------------

        self.ninducing = self.ninducing_preset

        if self.ninducing > self.obs_coords.shape[0]:
            self.ninducing = self.obs_coords.shape[0]
            self.inducing_coords = self.obs_coords
        else:
            init_size = 300
            if self.ninducing < init_size:
                init_size = self.ninducing
            kmeans = MiniBatchKMeans(init_size=init_size, n_clusters=self.ninducing, random_state=345)
            kmeans.fit(self.obs_coords)

            self.inducing_coords = kmeans.cluster_centers_

        # Kernel over items (used to construct priors over w and t)
        self.K_mm = self.kernel_func(self.inducing_coords, self.ls) # + 1e-6 * np.eye(self.ninducing) # jitter
        self.invK_mm = np.linalg.inv(self.K_mm)
        self.K_nm = self.kernel_func(self.obs_coords, self.ls, self.inducing_coords)

        # Related to w, the item components ------------------------------------------------------------
        # posterior expected values
        self.w_u = np.zeros((self.ninducing, self.Nfactors))
        # self.w_u = mvn.rvs(np.zeros(self.ninducing), self.K_mm, self.Nfactors).reshape(self.Nfactors, self.N)
        # self.w_u /= (self.shape_sw / self.rate_sw)[:, None]
        # self.w_u = self.w_u.T
        # self.w_u = np.zeros((self.ninducing, self.Nfactors))
        # self.w_u[np.arange(self.ninducing), np.arange(self.ninducing)] = 1.0

        # Prior covariance of w
        blocks = [self.K_mm for _ in range(self.Nfactors)]
        self.Kw_mm = block_diag(*blocks)
        blocks = [self.invK_mm for _ in range(self.Nfactors)]
        self.invKw_mm = block_diag(*blocks)
        blocks = [self.K_nm for _ in range(self.Nfactors)]
        self.Kw_nm = block_diag(*blocks)

        # moments of distributions over inducing points for convenience
        # posterior covariance
        self.wS = self.Kw_mm / self.shape_sw0 * self.rate_sw0
        self.winvS = self.invKw_mm * self.shape_sw0 / self.rate_sw0
        self.winvSm = np.zeros((self.ninducing * self.Nfactors, 1))
        self.w_cov_nm = self.Kw_nm / self.shape_sw0 * self.rate_sw0
        # self.wS = np.eye(self.Nfactors * self.ninducing)
        # self.w_cov_nm = np.eye(self.Nfactors * self.ninducing)

        # Inducing points for people -------------------------------------------------------------------
        if self.person_features is not None:
            #     self.use_svi_people = False

            self.y_ninducing = self.ninducing_preset

            if self.y_ninducing > self.Npeople or not self.use_person_svi:
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
            self.Ky_mm_block = self.y_kernel_func(self.y_inducing_coords, self.lsy)
            #self.Ky_mm_block += 1e-6 * np.eye(len(self.Ky_mm_block)) # jitter
            blocks = [self.Ky_mm_block for _ in range(self.Nfactors)]
            self.Ky_mm = block_diag(*blocks)

            # Related to y, the person components ----------------------------------------------------------
            # posterior means
            self.y_u = mvn.rvs(np.zeros(self.y_ninducing), self.Ky_mm_block, self.Nfactors)
            self.y_u /= (self.shape_sy / self.rate_sy)[:, None]

            # Prior covariance of y
            self.invKy_mm_block = np.linalg.inv(self.Ky_mm_block)
            blocks = [self.invKy_mm_block for _ in range(self.Nfactors)]
            self.invKy_mm = block_diag(*blocks)

            self.Ky_nm_block = self.y_kernel_func(self.person_features, self.lsy, self.y_inducing_coords)
            blocks = [self.Ky_nm_block for _ in range(self.Nfactors)]
            self.Ky_nm = block_diag(*blocks)

            # posterior covariance
            self.yS = self.Ky_mm / self.shape_sy0 * self.rate_sy0
            self.yinvS = self.invKy_mm * self.shape_sy0 / self.rate_sy0
            self.yinvSm = np.zeros((self.y_ninducing * self.Nfactors, 1))
            self.y_cov_nm = self.Ky_nm / self.shape_sy0 * self.rate_sy0
            self.y_cov = np.ones((self.Npeople * self.Nfactors, self.Npeople * self.Nfactors)) / self.shape_sy0 * self.rate_sy0

            self.Lambda = 0

        # Related to t, the item means -----------------------------------------------------------------
        self.t_u = np.zeros((self.ninducing, 1))  # posterior means

        if self.use_t:
            self.t_invSm = np.zeros((self.ninducing, 1), dtype=float)# theta_1/posterior covariance dot means
            self.t_invS = np.diag(np.ones(self.ninducing, dtype=float)) # theta_2/posterior covariance
            self.tS = np.diag(np.ones(self.ninducing, dtype=float))  # theta_2/posterior covariance
            self.t_mu0_u = np.zeros((self.ninducing, 1)) + self.t_mu0_u # prior means

            self.Kt_nm = np.tile(self.K_nm, (self.Npeople, 1))

    def _scaled_Kwy(self, K, K_nm, y_u, y_u_cov, y, y_cov_nm, inv_scale):

        N = K.shape[0]
        Npeople = y_u.shape[1]

        scaledK = np.zeros((N * Npeople, N * Npeople))

        Nout = K_nm.shape[0]
        Npeople_out = y.shape[1]
        scaledK_nm = np.zeros((Nout * Npeople_out, N * Npeople))

        for f in range(self.Nfactors):
            fidxs = np.arange(Npeople) + f * Npeople
            scaling = y_u[f:f + 1, :].T.dot(y_u[f:f + 1, :]) + y_u_cov[fidxs, :][:, fidxs]
            scaling = scaling[None, :, :, None]

            scaledK_f = K[:, None, None, :] * scaling
            scaledK_f = scaledK_f.reshape(N, Npeople, N * Npeople)
            scaledK_f = np.swapaxes(scaledK_f, 0, 2)
            scaledK_f = scaledK_f.reshape(N * Npeople, N * Npeople)

            scaledK_f /= inv_scale[f]

            scaledK += scaledK_f

            scaling = y[f:f + 1, :].T.dot(y_u[f:f + 1, :]) + y_cov_nm[np.arange(self.Npeople) + f * self.Npeople, :][:, fidxs]
            scaling = scaling[None, :, :, None]
            scaledK_nm_f = K_nm[:, None, None, :] * scaling
            scaledK_nm_f = scaledK_nm_f.reshape(Nout, Npeople_out, N * Npeople)
            scaledK_nm_f = np.swapaxes(scaledK_nm_f, 0, 1)
            scaledK_nm_f = scaledK_nm_f.reshape(Nout * Npeople_out, N * Npeople)

            scaledK_nm_f /= inv_scale[f]

            scaledK_nm += scaledK_nm_f

        scaled_invK = np.linalg.inv(scaledK)

        return scaledK, scaled_invK, scaledK_nm

    def _init_w(self):
        self.sw_matrix = np.ones(self.Kw_mm.shape) * self.shape_sw0 / float(self.rate_sw0)
        self.sw_nm = np.ones((self.Nfactors * self.N, self.Nfactors * self.ninducing)) * self.shape_sw0 \
                     / float(self.rate_sw0)

        # initialise the factors randomly -- otherwise they can get stuck because there is nothing to differentiate them
        # i.e. the cluster identifiability problem
        # self.w = np.zeros((self.N, self.Nfactors))
        self.w = self.K_nm.dot(self.invK_mm).dot(self.w_u)

        self.Sigma_w = np.zeros((self.ninducing, self.ninducing, self.Nfactors))

        if self.new_obs:
            self.wy_gp = GPPrefLearning(self.nitem_features, 0, self.shape_sw0, self.rate_sw0,
                                    self.shape_ls, self.rate_ls, self.ls,
                                    fixed_s=True, kernel_func='pre', use_svi=True,
                                    delay=self.delay, forgetting_rate=self.forgetting_rate,
                                    max_update_size=self.update_size)
            self.wy_gp.max_iter_VB_per_fit = 1
            self.wy_gp.min_iter_VB = 1
            self.wy_gp.max_iter_G = self.max_iter_G # G needs to converge within each VB iteration otherwise q(w) is very poor and crashes
            self.wy_gp.verbose = self.verbose
            self.wy_gp.conv_threshold = 1e-3
            self.wy_gp.conv_threshold_G = 1e-3
            self.wy_gp.conv_check_freq = 1

        # intialise Q using the prior covariance
        Kw_mm, invKw_mm, Kw_nm = self._scaled_Kwy(self.K_mm, self.K_nm,
                              np.zeros((self.Nfactors, self.y_ninducing)), self.Ky_mm / self.shape_sy0 * self.rate_sy0,
                              np.zeros((self.Nfactors, self.Npeople)), self.Ky_nm / self.shape_sy0 * self.rate_sy0,
                              self.shape_sw / self.rate_sw)

        self.dummy_inducing_coords = np.empty((self.ninducing * self.y_ninducing, 1))

        self.wy_gp.init_inducing_points(self.dummy_inducing_coords, Kw_mm, invKw_mm, Kw_nm)
        self.wy_gp.set_training_data(self.pref_v, self.pref_u, self.dummy_obs_coords, self.preferences,
                                     mu0=np.zeros((self.N*self.Npeople, 1)), K=None,
                                     process_obs=self.new_obs, input_type=self.input_type)

    def _init_t(self):
        self.t = np.zeros((self.N, 1))
        self.st = self.shape_st0 / self.rate_st0

        self.t_mu0 = np.zeros((self.N, 1)) + self.t_mu0

        if not self.use_t:
            return

        if self.new_obs:
            self.t_gp = GPPrefLearning(self.nitem_features, 0, 1, 1, self.shape_ls, self.rate_ls, self.ls,
                                       fixed_s=True, kernel_func='pre', use_svi=True,
                                       delay=self.delay, forgetting_rate=self.forgetting_rate,
                                       max_update_size=self.update_size)
            self.t_gp.max_iter_VB_per_fit = 1
            self.t_gp.min_iter_VB = 1
            self.t_gp.max_iter_G = self.max_iter_G  # G needs to converge within each VB iteration otherwise q(t) is poor
            self.t_gp.verbose = self.verbose
            self.t_gp.conv_threshold = 1e-3
            self.t_gp.conv_threshold_G = 1e-3
            self.t_gp.conv_check_freq = 1

        self.t_gp.vb_iter = 0
        self.t_gp.init_inducing_points(np.empty((self.ninducing, 1)), self.K_mm, self.invK_mm, self.Kt_nm)

    def _init_y(self):
        self.sy_matrix = np.ones(self.invKy_mm.shape) * self.shape_sy0 / float(self.rate_sy0)
        self.sy_nm = np.ones((self.Nfactors * self.Npeople, self.Nfactors * self.y_ninducing)) * self.shape_sy0 \
                     / float(self.rate_sy0)

        self.y = self.Ky_nm_block.dot(self.invKy_mm_block).dot(self.y_u.T).T

        self.Sigma_y = np.zeros((self.y_ninducing, self.y_ninducing, self.Nfactors))

    def _init_params(self):
        if self.Nfactors is None or self.Npeople < self.Nfactors:  # not enough items or people
            self.Nfactors = self.Npeople

        self._init_covariance()

        # initialise the inducing points first
        self._choose_inducing_points()

        self.ls = np.zeros(self.nitem_features) + self.ls

        self._init_w()
        self._init_y()
        self._init_t()

    def _process_observations(self, personIDs=None, items_1_coords=None, items_2_coords=None, item_features=None,
                              preferences=None, person_features=None, input_type='binary'):
        if person_features is None:
            self.use_person_svi = False
        else:
            self.use_person_svi = True

        super(CollabPrefLearningSVI, self)._process_observations(personIDs, items_1_coords, items_2_coords,
                                                             item_features, preferences, person_features, input_type)

    def _expec_t(self):

        self._update_sample()

        if not self.use_t:
            return

        N = self.ninducing

        mu0 = self.w.dot(self.y).T.reshape(self.N * self.Npeople, 1)

        self.t_gp.s = self.st
        self.t_gp.fit(self.pref_v, self.pref_u, self.dummy_obs_coords, self.preferences,
                      mu0=mu0, K=None, process_obs=self.new_obs, input_type=self.input_type)

        self.t_u = self.t_gp.um_minus_mu0
        self.tS = self.t_gp.uS

        self.t, _, _ = inducing_to_observation_moments(self.Kts_mm, self.invK_mm, self.K_nm, self.t_u, self.t_mu0)

        self.shape_st, self.rate_st = expec_output_scale(self.shape_st0, self.rate_st0, N,
                                                         self.invK_mm, self.t_u, np.zeros((N, 1)),
                                                         f_cov=self.tS)
        self.st = self.shape_st / self.rate_st

    def _compute_jacobian(self):
        self.obs_f = (self.w.dot(self.y) + self.t).T.reshape(self.N * self.Npeople, 1)

        phi, g_mean_f = pref_likelihood(self.obs_f, v=self.pref_v, u=self.pref_u, return_g_f=True)  # first order Taylor series approximation
        J = 1 / (2 * np.pi) ** 0.5 * np.exp(-g_mean_f ** 2 / 2.0) * np.sqrt(0.5)

        J = J[self.data_idx_i, :]
        s = (self.pref_v[self.data_idx_i, np.newaxis] == self.f_idx_i).astype(int) - \
            (self.pref_u[self.data_idx_i, np.newaxis] == self.f_idx_i).astype(int)

        J = J * s

        return J

    def _expec_w(self):
        """
        Compute the expectation over the latent features of the items and the latent personality components
        """
        # Put a GP prior on w with covariance K/gamma and mean 0
        N = self.ninducing
        Npeople = self.y_ninducing

        rho_i = (self.vb_iter + self.delay) ** (-self.forgetting_rate)
        w_i = np.sum(self.wy_gp.obs_total_counts) / float(np.sum(self.wy_gp.obs_total_counts[self.data_idx_i]))

        self.wy_gp.data_obs_idx_i = self.data_idx_i

        G = -np.inf

        self.prev_winvS = self.winvS
        self.prev_winvSm = self.winvSm

        for G_iter in range(self.max_iter_G):

            oldG = G
            G = self._compute_jacobian()

            # we need to map from the real Npeople points to the inducing points.
            Lambda_factor1 = self.covpair_i.dot(G.T)
            # N*self.Npeople x N*self.Npeople
            Lambda_i = (Lambda_factor1 / self.wy_gp.Q[None, self.data_idx_i]).dot(Lambda_factor1.T)
            Lambda_i = Lambda_i.reshape(Npeople, N, Npeople, N)
            invQ = Lambda_i #_scaled

            w_prec = np.zeros((N * self.Nfactors, N * self.Nfactors))
            for f in range(self.Nfactors):
                for g in range(self.Nfactors):
                    #  is to update each factor in turn, which may do a better job of cancelling out unneeded factors.
                    yscaling = self.y_u[f:f+1, :].T.dot(self.y_u[g:g+1, :]) + self.yS[f * Npeople + np.arange(Npeople),
                                                                              :][:, g * Npeople + np.arange(Npeople)]

                    Sigma_f_g = np.sum(np.sum(yscaling[:, None, :, None] * invQ, 2), 0)

                    fidxs = np.tile(f * N + np.arange(N)[:, None], (1, N))
                    gidxs = np.tile(g * N + np.arange(N)[None, :], (N, 1))
                    w_prec[fidxs, gidxs] = Sigma_f_g

                    if f == g:
                        self.Sigma_w[:, :, f] = Sigma_f_g

            # need to get invS for current iteration and merge using SVI weighted sum
            self.winvS = (1-rho_i) * self.prev_winvS + rho_i * (self.invKw_mm * self.sw_matrix + w_i * w_prec)

            mu0_i = np.tile(self.t, (self.Npeople, 1))[self.f_idx_i, :]
            z0 = pref_likelihood(self.obs_f, v=self.pref_v[self.data_idx_i], u=self.pref_u[self.data_idx_i]) \
                 + G.dot(mu0_i - self.obs_f[self.f_idx_i])

            invQ_f = (Lambda_factor1 / self.wy_gp.Q[None, self.data_idx_i]).dot(self.wy_gp.z[self.data_idx_i] - z0) # N Npeople x 1
            x = self.y_u.dot(invQ_f.reshape(Npeople, N)) # should be able to calculate over only subsample of people in the current round
            x = x.reshape(N * self.Nfactors, 1)

            # need to get x for current iteration and merge using SVI weighted sum
            self.winvSm = (1-rho_i) * self.prev_winvSm + rho_i * w_i * x

            self.wS = np.linalg.inv(self.winvS)
            self.w_u = self.wS.dot(self.winvSm)

            self.w, _, _ = inducing_to_observation_moments(None, self.invKw_mm, self.Kw_nm, self.w_u, 0)

            self.w_u = np.reshape(self.w_u, (self.Nfactors, N)).T  # w is N x Nfactors
            self.w = np.reshape(self.w, (self.Nfactors, self.N)).T  # w is N x Nfactors

            diff = np.max(np.abs(oldG - G))
            if diff < self.conv_threshold_G:
                break

            if self.verbose:
                logging.debug("expec_w: iter %i, diff=%f" % (G_iter, diff))

        for f in range(self.Nfactors):
            fidxs = np.arange(N) + (N * f)
            self.shape_sw[f], self.rate_sw[f] = expec_output_scale(self.shape_sw0, self.rate_sw0, N,
                                                       self.invK_mm, self.w_u[:, f:f + 1], np.zeros((N, 1)),
                                                       f_cov=self.wS[fidxs, :][:, fidxs])

            self.sw_matrix[fidxs, :] = self.shape_sw[f] / self.rate_sw[f]

            fidxs = np.arange(self.N) + (self.N * f)
            self.sw_nm[fidxs, :] = self.shape_sw[f] / self.rate_sw[f]

    def _expec_y(self):

        N = self.ninducing
        Npeople = self.y_ninducing

        rho_i = (self.vb_iter + self.delay) ** (-self.forgetting_rate)
        w_i = np.sum(self.wy_gp.obs_total_counts) / float(np.sum(self.wy_gp.obs_total_counts[self.data_idx_i]))

        self.wy_gp.data_obs_idx_i = self.data_idx_i

        self.prev_yinvSm = self.yinvSm
        self.prev_yinvS = self.yinvS
        self.prev_Lambda = self.Lambda

        G = -np.inf

        for G_iter in range(self.max_iter_G):
            oldG = G
            G = self._compute_jacobian()

            Lambda_factor1 = self.covpair_i.dot(G.T)
            Lambda_i = (Lambda_factor1 / self.wy_gp.Q[None, self.data_idx_i]).dot(Lambda_factor1.T)

            invQ = Lambda_i.reshape(Npeople, N, Npeople, N)

            Lambda_i_scaled = np.zeros((Npeople, N, Npeople, N))
            # for f in range(self.Nfactors):
            #     scaling = self.w_u[:, f:f+1].dot(self.w_u[:, f:f+1].T) + self.wS[N * f + np.arange(N), :][:,
            #                                                            N * f + np.arange(N)]
            #     scaling = scaling[None, :, None, :]
            #     Lambda_i_scaled_f = Lambda_i * scaling
            #     Lambda_i_scaled += Lambda_i_scaled_f

            y_prec = np.zeros((self.Nfactors * Npeople, self.Nfactors * Npeople))
            for f in range(self.Nfactors):
                for g in range(self.Nfactors):
                    wscaling = self.wS[f * N + np.arange(N), :][:, g * N + np.arange(N)] + \
                               self.w_u[:, f:f+1].dot(self.w_u[:, g:g+1].T)

                    invQ_scaled_fg = wscaling[None, :, None, :] * invQ
                    Sigma_f_g = np.sum(np.sum(invQ_scaled_fg, 3), 1)

                    Lambda_i_scaled += invQ_scaled_fg

                    fidxs = np.tile(f * Npeople + np.arange(Npeople)[:, None], (1, Npeople))
                    gidxs = np.tile(g * Npeople + np.arange(Npeople)[None, :], (Npeople, 1))

                    y_prec[fidxs, gidxs] = Sigma_f_g

                    if f == g:
                        self.Sigma_y[:, :, f] = Sigma_f_g

            # need to get invS for current iteration and merge using SVI weighted sum
            self.yinvS = (1-rho_i) * self.prev_yinvS + rho_i * (self.invKy_mm * self.sy_matrix + w_i * y_prec)
            self.Lambda = (1-rho_i) * self.prev_Lambda + rho_i * w_i * Lambda_i.reshape(N*Npeople, N*Npeople)

            mu0_i = np.tile(self.t, (self.Npeople, 1))[self.f_idx_i, :]
            z0 = pref_likelihood(self.obs_f, v=self.pref_v[self.data_idx_i], u=self.pref_u[self.data_idx_i]) \
                 + G.dot(mu0_i - self.obs_f[self.f_idx_i])

            invQ_f = (Lambda_factor1 / self.wy_gp.Q[None, self.data_idx_i]).dot(self.wy_gp.z[self.data_idx_i] - z0)
            x = self.w_u.T.dot(invQ_f.reshape(Npeople, N).T) # here we sum over items
            x = x.reshape(Npeople * self.Nfactors, 1)

            # need to get x for current iteration and merge using SVI weighted sum
            self.yinvSm = (1-rho_i) * self.prev_yinvSm + rho_i * w_i * x

            self.yS = np.linalg.inv(self.yinvS)
            self.y_u = self.yS.dot(self.yinvSm)

            self.y, _, _ = inducing_to_observation_moments(None, self.invKy_mm, self.Ky_nm, self.y_u, 0)
            self.y_u = np.reshape(self.y_u, (self.Nfactors, Npeople))  # y is Npeople x Nfactors
            self.y = np.reshape(self.y, (self.Nfactors, self.Npeople))  # y is Npeople x Nfactors

            diff = np.max(np.abs(oldG - G))
            if diff < self.conv_threshold_G:
                break

            if self.verbose:
                logging.debug("expec_y: iter %i, diff=%f" % (G_iter, diff))

        for f in range(self.Nfactors):
            fidxs = np.arange(Npeople) + (Npeople * f)
            self.shape_sy[f], self.rate_sy[f] = expec_output_scale(self.shape_sy0, self.rate_sy0, Npeople,
                                                                   self.invKy_mm_block, self.y_u[f:f + 1, :].T,
                                                                   np.zeros((Npeople, 1)),
                                                                   f_cov=self.yS[fidxs, :][:, fidxs])

            self.sy_matrix[fidxs, :] = self.shape_sy[f] / self.rate_sy[f]  # sy_rows

            fidxs = np.arange(self.Npeople) + (Npeople * f)
            self.sy_nm[fidxs, :] = self.shape_sy[f] / self.rate_sy[f]

    def _update_sample_idxs(self):
        self.data_idx_i = np.sort(np.random.choice(len(self.preferences), self.update_size, replace=False))
        self.f_idx_i = np.zeros((self.N, self.Npeople), dtype=bool)
        self.f_idx_i[self.tpref_v[self.data_idx_i], self.personIDs[self.data_idx_i]] = True
        self.f_idx_i[self.tpref_u[self.data_idx_i], self.personIDs[self.data_idx_i]] = True

        self.f_idx_i = self.f_idx_i.T.reshape(self.N * self.Npeople)
        self.f_idx_i = np.argwhere(self.f_idx_i).flatten()

    def _update_sample(self):

        self._update_sample_idxs()

        self.Kws_mm = self.Kw_mm / self.sw_matrix
        self.inv_Kws_mm  = self.invKw_mm * self.sw_matrix
        self.Kws_nm = self.Kw_nm  / self.sw_nm

        if self.use_t:
            self.Kts_mm = self.K_mm / self.st
            self.inv_Kts_mm  = self.invK_mm * self.st
            self.Kts_nm = self.Kt_nm / self.st

        self.Kys_mm = self.Ky_mm / self.sy_matrix
        self.inv_Kys_mm  = self.invKy_mm * self.sy_matrix
        self.Kys_nm = self.Ky_nm / self.sy_nm

        N = self.ninducing
        Npeople = self.y_ninducing
        covpair = self.invK_mm.dot(self.K_nm.T)[None, :, None, :]
        covpair = covpair * self.invKy_mm_block.dot(self.Ky_nm_block.T)[:, None, :, None]
        covpair = covpair.reshape(N*Npeople, self.N * self.Npeople)
        self.covpair_i = covpair[:, self.f_idx_i]

    def _logpD(self):

        rho = self.predict(self.personIDs, self.tpref_v, self.tpref_u, self.obs_coords, self.person_features, no_var=True)
        rho = temper_extreme_probs(rho)
        logrho = np.log(rho)
        lognotrho = np.log(1 - rho)

        prod_cov = 0
        y_w_cov_y = 0
        w_y_cov_w = 0
        for f in range(self.Nfactors):

            fidxs = np.arange(self.ninducing) + (self.ninducing * f)
            w_cov = self.wS[fidxs, :][:, fidxs]

            fidxs = np.arange(self.y_ninducing) + (self.y_ninducing * f)
            y_cov = self.yS[fidxs, :][:, fidxs]

            cov = w_cov[None, :, None, :] * y_cov[:, None, :, None]
            cov = cov.reshape(self.ninducing * self.y_ninducing, self.ninducing * self.y_ninducing)

            y_w_cov_y_f = w_cov[None, :, None, :] * self.y_u[f:f+1, :].T.dot(self.y_u[f:f+1, :])[:, None, :, None]
            y_w_cov_y_f = y_w_cov_y_f.reshape(self.ninducing * self.y_ninducing, self.ninducing * self.y_ninducing)
            y_w_cov_y += y_w_cov_y_f

            w_y_cov_w_f = y_cov[:, None, :, None] * self.w_u[:, f:f+1].dot(self.w_u[:, f:f+1].T)[None, :, None, :]
            w_y_cov_w_f = w_y_cov_w_f.reshape(self.ninducing * self.y_ninducing, self.ninducing * self.y_ninducing)
            w_y_cov_w += w_y_cov_w_f

            prod_cov += cov

        data_ll = self.wy_gp.data_ll(logrho, lognotrho)
        data_ll -= 0.5 * np.trace((prod_cov + w_y_cov_w + y_w_cov_y).dot(self.Lambda))

        return data_ll

    def lowerbound(self):

        data_ll = self._logpD()

        Elnsw = psi(self.shape_sw) - np.log(self.rate_sw)
        Elnsy = psi(self.shape_sy) - np.log(self.rate_sy)
        if self.use_t:
            Elnst = psi(self.shape_st) - np.log(self.rate_st)
            st = self.st
        else:
            Elnst = 0
            st = 1

        sw = self.shape_sw / self.rate_sw
        sy = self.shape_sy / self.rate_sy

        # the parameter N is not multiplied here by Nfactors because it will be multiplied by the s value for each
        # factor and summed inside the function
        logpw = expec_pdf_gaussian(self.Kw_mm, self.invKw_mm, Elnsw, self.ninducing, self.sw_matrix,
                                   self.w_u.T.reshape(self.ninducing * self.Nfactors, 1), 0, self.wS, 0)
        logqw = expec_q_gaussian(self.wS, self.ninducing * self.Nfactors)

        if self.use_t:
            logpt = expec_pdf_gaussian(self.K_mm, self.invK_mm, Elnst, self.ninducing, st, self.t_u, self.t_mu0_u,
                                       0, 0) - 0.5 * self.ninducing
            logqt = expec_q_gaussian(self.tS, self.ninducing)
        else:
            logpt = 0
            logqt = 0

        logpy = expec_pdf_gaussian(self.Ky_mm, self.invKy_mm, Elnsy, self.y_ninducing, self.sy_matrix,
                                   self.y_u.reshape(self.y_ninducing * self.Nfactors, 1), 0, self.yS, 0)
        logqy = expec_q_gaussian(self.yS, self.y_ninducing * self.Nfactors)

        # if self.nperson_features is not None:
        # else:
            # logpy = 0
            # for f in range(self.Nfactors):
                # logpy += np.sum(norm.logpdf(self.y[f, :], scale=np.sqrt(self.rate_sy[f] / self.shape_sy[f])))
            # logqy = mvn.logpdf(self.y.flatten(), mean=self.y.flatten(), cov=self.y_cov)

        logps_y = 0
        logqs_y = 0
        logps_w = 0
        logqs_w = 0
        for f in range(self.Nfactors):
            logps_w += lnp_output_scale(self.shape_sw0, self.rate_sw0, self.shape_sw[f], self.rate_sw[f], sw[f],
                                        Elnsw[f])
            logqs_w += lnq_output_scale(self.shape_sw[f], self.rate_sw[f], sw[f], Elnsw[f])

            logps_y += lnp_output_scale(self.shape_sy0, self.rate_sy0, self.shape_sy[f], self.rate_sy[f], sy[f],
                                        Elnsy[f])
            logqs_y += lnq_output_scale(self.shape_sy[f], self.rate_sy[f], sy[f], Elnsy[f])

        logps_t = lnp_output_scale(self.shape_st0, self.rate_st0, self.shape_st, self.rate_st, st, Elnst)
        logqs_t = lnq_output_scale(self.shape_st, self.rate_st, st, Elnst)

        w_terms = logpw - logqw + logps_w - logqs_w
        y_terms = logpy - logqy + logps_y - logqs_y
        t_terms = logpt - logqt + logps_t - logqs_t

        lb = data_ll + t_terms + w_terms + y_terms

        if self.verbose:
            logging.debug('s_w=%s' % (self.shape_sw / self.rate_sw))
            logging.debug('s_y=%s' % (self.shape_sy / self.rate_sy))
            logging.debug('s_t=%.2f' % (self.shape_st / self.rate_st))

        if self.verbose:
            logging.debug('likelihood=%.3f, wterms=%.3f, yterms=%.3f, tterms=%.3f' % (data_ll, w_terms, y_terms, t_terms))

        logging.debug("Iteration %i: Lower bound = %.3f, " % (self.vb_iter, lb))

        if self.verbose:
            logging.debug("t: %.2f, %.2f" % (np.min(self.t), np.max(self.t)))
            logging.debug("w: %.2f, %.2f" % (np.min(self.w), np.max(self.w)))
            logging.debug("y: %.2f, %.2f" % (np.min(self.y), np.max(self.y)))

        return lb

    def _predict_w_t(self, coords_1):

        # kernel between pidxs and t
        K = self.kernel_func(coords_1, self.ls, self.inducing_coords)
        K_starstar = self.kernel_func(coords_1, self.ls, coords_1)
        covpair = K.dot(self.invK_mm)
        N = coords_1.shape[0]

        # use kernel to compute t.
        if self.use_t:
            t_out = K.dot(self.invK_mm).dot(self.t_u)

            covpair_uS = covpair.dot(self.tS)
            cov_t = K_starstar * self.rate_st / self.shape_st + (covpair_uS - covpair.dot(self.Kts_mm)).dot(covpair.T)
        else:
            t_out = np.zeros((N, 1))

            cov_t = np.zeros((N, N))


        # kernel between pidxs and w -- use kernel to compute w. Don't need Kw_mm block-diagonal matrix
        w_out = K.dot(self.invK_mm).dot(self.w_u)

        cov_w = np.zeros((self.Nfactors, N, N))
        for f in range(self.Nfactors):
            fidxs = np.arange(self.ninducing) + self.ninducing * f
            cov_w[f] = K_starstar  * self.rate_sw[f] / self.shape_sw[f] + \
               covpair.dot(self.wS[fidxs, :][:, fidxs] - self.K_mm * self.rate_sw[f] / self.shape_sw[f]).dot(covpair.T)

        return t_out, w_out, cov_t, cov_w

    def _predict_y(self, person_features):

        Ky = self.y_kernel_func(person_features, self.lsy, self.y_inducing_coords)
        Ky_starstar = self.y_kernel_func(person_features, self.lsy, person_features)
        covpair = Ky.dot(self.invKy_mm_block)
        Npeople = person_features.shape[0]

        y_out = Ky.dot(self.invKy_mm_block).dot(self.y_u.T).T

        cov_y = np.zeros((self.Nfactors, Npeople, Npeople))
        for f in range(self.Nfactors):
            fidxs = np.arange(self.y_ninducing) + self.y_ninducing * f
            cov_y[f] = Ky_starstar * self.rate_sy[f] / self.shape_sy[f] + covpair.dot(self.yS[fidxs, :][:, fidxs]
                                                    - self.Ky_mm_block * self.rate_sy[f] / self.shape_sy[f]).dot(covpair.T)

        return y_out, cov_y

    def _gradient_dim(self, lstype, d, dimension):
        der_logpw_logqw = 0
        der_logpy_logqy = 0
        der_logpt_logqt = 0
        der_logpf_logqf = 0

        # compute the gradient. This should follow the MAP estimate from chu and ghahramani.
        # Terms that don't involve the hyperparameter are zero; implicit dependencies drop out if we only calculate
        # gradient when converged due to the coordinate ascent method.
        if lstype == 'item' or (lstype == 'both' and d < self.nitem_features):
            dKdls = self.K_mm * self.kernel_der(self.inducing_coords, self.ls, dimension)
            # try to make the s scale cancel as much as possible
            invK_w = self.invK_mm.dot(self.w_u)
            invKs_C = (self.invKw_mm * self.sw_matrix).dot(self.wS)
            N = self.ninducing

            for f in range(self.Nfactors):
                fidxs = np.arange(N) + (N * f)

                swf = self.shape_sw[f] / self.rate_sw[f]
                invKs_Cf = invKs_C[fidxs, :][:, fidxs]
                invK_wf = invK_w[:, f]

                Sigma = self.Sigma_w[:, :, f]

                der_logpw_logqw += 0.5 * (invK_wf.T.dot(dKdls).dot(invK_wf) * swf -
                                    np.trace(invKs_Cf.dot(Sigma).dot(dKdls / swf)))

            if self.use_t:
                invKs_t = self.invK_mm.dot(self.t_u) * self.st
                invKs_C = self.invK_mm.dot(self.tS) * self.st

                der_logpt_logqt = 0.5 * (invKs_t.T.dot(dKdls).dot(invKs_t) -
                            np.trace(invKs_C.dot(self.t_gp.get_obs_precision()).dot(dKdls / self.st)))

        elif (lstype == 'person' or (lstype == 'both' and d >= self.nitem_features)) and self.person_features is None:
            dKdls = self.Ky_mm_block * self.kernel_der(self.y_inducing_coords, self.lsy, dimension)
            invK_y = self.invKy_mm_block.dot(self.y_u.T)

            invKs_C = (self.invKy_mm * self.sy_matrix).dot(self.yS)
            N = self.y_ninducing

            for f in range(self.Nfactors):
                fidxs = np.arange(N) + (N * f)

                syf = self.shape_sy[f] / self.rate_sy[f]
                invKs_Cf = invKs_C[fidxs, :][:, fidxs]
                invK_yf = invK_y[:, f]

                Sigma = self.Sigma_y[:, :, f]

                der_logpy_logqy += 0.5 * (invK_yf.T.dot(dKdls).dot(invK_yf) * syf -
                                    np.trace(invKs_Cf.dot(Sigma).dot(dKdls / syf)))

        return der_logpw_logqw + der_logpy_logqy + der_logpt_logqt + der_logpf_logqf
