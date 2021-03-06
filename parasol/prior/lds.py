import numpy as np
from deepx import T, stats

from .common import Dynamics

__all__ = ['LDS']

class LDS(Dynamics):

    def __init__(self, ds, da, horizon, time_varying=False, smooth=False):
        super(LDS, self).__init__(ds, da, horizon)
        self.time_varying = time_varying
        self.smooth = smooth
        self.cache = {}
        self.initialize_objective()

    def encode(self, q_X, q_A, dynamics_stats=None):
        if self.smooth:
            state_prior = stats.Gaussian([
                T.eye(self.ds),
                T.zeros(self.ds)
            ])
            if dynamics_stats is None:
                dynamics_stats = self.sufficient_statistics()
            q_X = stats.LDS(
                (dynamics_stats, state_prior, q_X, q_A.expected_value(), self.horizon)
            )
        return q_X, q_A

    def is_filtering_prior(self):
        return self.smooth

    def initialize_objective(self):
        H, ds, da = self.horizon, self.ds, self.da
        if self.time_varying:
            A = T.concatenate([T.eye(ds), T.zeros([ds, da])], -1)
            self.A = T.variable(A[None] + 1e-2 * T.random_normal([H - 1, ds, ds + da]))
            self.Q_log_diag = T.variable(T.random_normal([H - 1, ds]) + 1)
            self.Q = T.matrix_diag(T.exp(self.Q_log_diag))
        else:
            A = T.concatenate([T.eye(ds), T.zeros([ds, da])], -1)
            self.A = T.variable(A + 1e-2 * T.random_normal([ds, ds + da]))
            self.Q_log_diag = T.variable(T.random_normal([ds]) + 1)
            self.Q = T.matrix_diag(T.exp(self.Q_log_diag))

    def sufficient_statistics(self):
        A, Q = self.get_dynamics()
        Q_inv = T.matrix_inverse(Q)
        Q_inv_A = T.matrix_solve(Q, A)
        return [
            -0.5 * Q_inv,
            Q_inv_A,
            -0.5 * T.einsum('hba,hbc->hac', A, Q_inv_A),
            -0.5 * T.logdet(Q)
        ]

    def forward(self, q_Xt, q_At):
        Xt, At = q_Xt.expected_value(), q_At.expected_value()
        batch_size = T.shape(Xt)[0]
        XAt = T.concatenate([Xt, At], -1)
        A, Q = self.get_dynamics()
        p_Xt1 = stats.Gaussian([
            T.tile(Q[None], [batch_size, 1, 1, 1]),
            T.einsum('nhs,hxs->nhx', XAt, A)
        ])
        return p_Xt1

    def get_dynamics(self):
        if self.time_varying:
            return self.A, self.Q
        else:
            return (
                T.tile(self.A[None], [self.horizon - 1, 1, 1]),
                T.tile(self.Q[None], [self.horizon - 1, 1, 1])
            )

    def get_parameters(self):
        return [self.A, self.Q_log_diag]

    def __getstate__(self):
        state = super(LDS, self).__getstate__()
        state['time_varying'] = self.time_varying
        state['weights'] = T.get_current_session().run(self.get_parameters())
        return state

    def __setstate__(self, state):
        time_varying = state.pop('time_varying')
        weights = state.pop('weights')
        self.__init__(state['ds'], state['da'], state['horizon'], time_varying=time_varying)
        T.get_current_session().run([T.core.assign(a, b) for a, b in zip(self.get_parameters(), weights)])

    def get_statistics(self, q_Xt, q_At, q_Xt1):
        Xt1_Xt1T, Xt1 = stats.Gaussian.unpack(q_Xt1.expected_sufficient_statistics())

        Xt_XtT, Xt = stats.Gaussian.unpack(q_Xt.expected_sufficient_statistics())
        At_AtT, At = stats.Gaussian.unpack(q_At.expected_sufficient_statistics())

        XtAt = T.concatenate([Xt, At], -1)
        XtAt_XtAtT = T.concatenate([
            T.concatenate([Xt_XtT, T.outer(Xt, At)], -1),
            T.concatenate([T.outer(At, Xt), At_AtT], -1),
        ], -2)
        return (XtAt_XtAtT, XtAt), (Xt1_Xt1T, Xt1)

    def kl_divergence(self, q_X, q_A, _):
        # q_Xt - [N, H, ds]
        # q_At - [N, H, da]
        if (q_X, q_A) not in self.cache:
            info = {}
            if self.smooth:
                state_prior = stats.GaussianScaleDiag([
                    T.ones(self.ds),
                    T.zeros(self.ds)
                ])
                p_X = stats.LDS(
                    (self.sufficient_statistics(), state_prior, None, q_A.expected_value(), self.horizon),
                'internal')
                kl = T.mean(stats.kl_divergence(q_X, p_X), axis=0)
                Q = self.get_dynamics()[1]
                info['model-stdev'] = T.sqrt(T.matrix_diag_part(Q))
            else:
                q_Xt = q_X.__class__([
                    q_X.get_parameters('regular')[0][:, :-1],
                    q_X.get_parameters('regular')[1][:, :-1],
                ])
                q_At = q_A.__class__([
                    q_A.get_parameters('regular')[0][:, :-1],
                    q_A.get_parameters('regular')[1][:, :-1],
                ])
                p_Xt1 = self.forward(q_Xt, q_At)
                q_Xt1 = q_X.__class__([
                    q_X.get_parameters('regular')[0][:, 1:],
                    q_X.get_parameters('regular')[1][:, 1:],
                ])
                rmse = T.sqrt(T.sum(T.square(q_Xt1.get_parameters('regular')[1] - p_Xt1.get_parameters('regular')[1]), axis=-1))
                kl = T.mean(T.sum(stats.kl_divergence(q_Xt1, p_Xt1), axis=-1), axis=0)
                Q = self.get_dynamics()[1]
                model_stdev = T.sqrt(T.matrix_diag_part(Q))
                info['rmse'] = rmse
                info['model-stdev'] = model_stdev
            self.cache[(q_X, q_A)] = kl, info
        return self.cache[(q_X, q_A)]

    def kl_gradients(self, q_X, q_A, kl, num_data):
        return T.grad(kl, self.get_parameters())

    def next_state(self, state, action, t):
        A, Q = self.get_dynamics()
        leading_dim = T.shape(state)[:-1]
        state_action = T.concatenate([state, action], -1)
        return stats.Gaussian([
            T.tile(Q[t][None], T.concatenate([leading_dim, [1, 1]])),
            T.einsum('ab,nb->na', A[t], state_action)
        ])
