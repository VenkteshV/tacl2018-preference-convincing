import numpy as np
from scipy.stats import norm

from gp_pref_learning import GPPrefLearning


class GPPrefPerUser():
    '''
    Runs a separate preference learning model for each user. I.e. multiple users but no collaborative learning.
    '''

    def __init__(self, Npeople, max_update_size, shape_s0, rate_s0, nitem_feats=2):
        self.user_models = []
        self.Npeople = Npeople
        for p in range(Npeople):
            model_p = GPPrefLearning(nitem_feats, mu0=0, shape_s0=shape_s0, rate_s0=rate_s0, ls_initial=None, use_svi=True, ninducing=50,
                                     max_update_size=max_update_size, forgetting_rate=0.9, verbose=True)

            self.user_models.append(model_p)


    def fit(self, users, p1, p2, item_features, prefs, _, optimize, use_median_ls):

        uusers = np.unique(users)
        for u in uusers:
            uidxs = users == u

            self.user_models[u].fit(
                p1[uidxs],
                p2[uidxs],
                item_features,
                prefs[uidxs],
                optimize=optimize,
                use_median_ls=use_median_ls
            )

    def predict_f(self, item_features, chosen_users=None):

        fpred = []

        if chosen_users is None:
            chosen_users = range(self.Npeople)

        for u in chosen_users:

            if self.user_models[u].vb_iter == 0:
                # not trained, skip it
                fpredu = np.zeros((item_features.shape[0], 1))
            else:
                fpredu, _ = self.user_models[u].predict_f(item_features)

            fpred.append(fpredu)

        fpred = np.concatenate(fpred, axis=1)

        return fpred

    def predict(self, users, p1, p2, item_features, _):

        rhopred = np.zeros(len(p1))
        varrhopred = np.zeros(len(p1))

        uusers = np.unique(users)
        for u in uusers:
            uidxs = users.flatten() == u

            rho_pred_u, var_rho_pred_u = self.user_models[u].predict(item_features, p1[uidxs], p2[uidxs])
            rhopred[uidxs] = rho_pred_u.flatten()
            varrhopred[uidxs] = var_rho_pred_u.flatten()

        return rhopred

    def predict_t(self, item_features):
        F = self.predict_f(item_features, None)
        return np.mean(F, axis=1)

    def predict_common(self, item_features, p1, p2):
        F = self.predict_f(item_features, None)

        # predict the common mean/consensus or underlying ground truth function
        g_f = (np.mean(F[p1], axis=1) - np.mean(F[p2], axis=1)).astype(int) / np.sqrt(2)
        phi = norm.cdf(g_f)
        return phi

    def lowerbound(self):
        lb = 0
        for model in self.user_models:
            lb += model.lowerbound()

        return lb