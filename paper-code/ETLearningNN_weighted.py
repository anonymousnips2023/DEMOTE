import torch
import numpy as np
from torch.nn import Module, Sequential, Linear, Tanh, Parameter, ModuleList, ParameterList
# from torchdiffeq import odeint_adjoint as odeint
from torchdiffeq import odeint
from torch.optim import Adam
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.linalg import solve, cholesky, inv

from torch.utils import data as data_utils
from itertools import islice
from matplotlib import pyplot as plt
from tqdm import tqdm

np.random.seed(0)
torch.random.manual_seed(0)
torch.set_default_tensor_type(torch.DoubleTensor)

class KernelRBF:
    def __init__(self, jitter=1e-5):
        self.jitter = jitter

    def cross3(self, X1, X2, ls):
        X1_norm = (X1 ** 2).sum(2, keepdim=True)
        X2_norm = (X2 ** 2).sum(2, keepdim=True)

        K = X1_norm - 2 * X1 @ X2.transpose(1, 2) + X2_norm.transpose(1, 2)
        K = K.unsqueeze(0)
        ls = ls.view((-1, 1, 1, 1))
        K = torch.exp(-0.5 * K / ls)
       
        return K
    
    def matrix3(self, X, ls):
        K = self.cross3(X, X, ls)
        K = K + self.jitter * torch.eye(K.shape[-1], dtype=torch.float32, device=X.device).unsqueeze(0).unsqueeze(0)
        return K

    def cross2(self, X1, X2, ls):
        X1_norm = (X1 ** 2).sum(1).reshape((-1, 1))
        X2_norm = (X2 ** 2).sum(1).reshape((-1, 1))
        K = X1_norm - 2 * X1 @ X2.T + X2_norm.T
        K = K.unsqueeze(0)
        ls = ls.view((-1, 1, 1))
        K = torch.exp(-0.5 * K / ls)
        return K

    def matrix2(self, X, ls):
        K = self.cross2(X, X, ls)
        K = K + self.jitter * torch.eye(X.shape[0], dtype=torch.float32, device=X.device).unsqueeze(0)
        return K

    def cross(self, X1, X2, ls):
        X1_norm = (X1 ** 2).sum(1).reshape((-1, 1))
        X2_norm = (X2 ** 2).sum(1).reshape((-1, 1))
        K = X1_norm - 2 * X1 @ X2.T + X2_norm.T
        K = torch.exp(-0.5 * K / ls)
        return K

    def matrix(self, X, ls):
        K = self.cross(X, X, ls)
        K = K + self.jitter * torch.eye(X.shape[0], dtype=torch.float32, device=X.device)
        return K

class Diffusion(Module):
    def __init__(self, num_node):
        super(Diffusion, self).__init__()
        self.num_node = num_node
        self.W = Parameter(torch.tensor(np.random.rand(self.num_node, self.num_node)))
        self.mask = None

    def forward(self, U):
        Wtril = torch.tril(self.W)
        Wtril = Wtril * self.mask
        W = Wtril + Wtril.T
        D = torch.diag(W.sum(1))
        diff = (W - D) @ U
        return diff

class NN(Module):
    def __init__(self, layers):
        super(NN, self).__init__()
        self.layers = layers
        self.act = Tanh()
        self.fc = ModuleList()
        for i in range(len(self.layers)-1):
            self.fc.append(Linear(self.layers[i], self.layers[i+1]))
    
    def forward(self, X):
        for i in range(len(self.layers) - 2):
            X = self.act(self.fc[i](X))
        X = self.fc[-1](X)
        return X
    
    def reg(self):
        reg = 0
        for fc in self.fc:
            reg += (fc.weight ** 2).sum()
            reg += (fc.bias ** 2).sum()
        return reg

class ODEFunction(Module):
    def __init__(self, nvec, num_node, reaction_layers):
        super(ODEFunction, self).__init__()

        self.num_node = num_node
        self.nvec = nvec
        self.nmod = len(self.nvec)
        # self.reaction = NN(reaction_layers)
        self.diffusion = Diffusion(num_node)
        self.reaction = ModuleList()
        for k in range(self.nmod):
            # self.reaction.append(SparseGP(num_pseudo, dim_input+1, dim_output))
            self.reaction.append(NN(reaction_layers))
        self.w = Parameter(torch.tensor(0.))
        # self.is_train = True

    def forward(self, t, u):
        u = u.view((self.num_node, -1))
        term_diff = self.diffusion(u)
        X = torch.cat([u, t.repeat(self.num_node).view((-1, 1))], 1)
        X = torch.split(X, self.nvec, dim=0)
        term_react = []
        for k in range(self.nmod):
            react = self.reaction[k](X[k])
            # print(react.shape)
            term_react.append(react)

        # term_react = self.reaction(X)
        term_react = torch.cat(term_react, 0)
        a = torch.sigmoid(self.w)
        d = term_diff * a + term_react * (1 - a)
        d = d.view(-1)
        return d
    
    def reg(self):
        reg = 0
        for reaction in self.reaction:
            reg += reaction.reg()
        return reg
    

class ETL(Module):
    def __init__(self, nvec, dim_embedding, num_pseudo, reaction_layers,  device=torch.device('cuda:0')):
        super(ETL, self).__init__()
        self.device = device
        self.dim_embedding = dim_embedding # embedding dimension
        self.nvec = nvec
        self.nmod = len(nvec)
        self.num_node = np.sum(nvec)
        self.num_pseudo = num_pseudo
        self.IC = Parameter(torch.tensor(np.random.rand(self.num_node, self.dim_embedding)))
        # self.ode_func = ODEFunction(self.nvec, self.num_node, self.dim_embedding, self.dim_embedding, self.num_pseudo, diagonal)
        self.ode_func = ODEFunction(self.nvec, self.num_node, reaction_layers)
        # self.f = SparseGP(self.num_pseudo, self.dim_embedding * self.nmod, 1)
        self.f = NN([self.nmod * self.dim_embedding, 50, 50, 1])
        # self.cp_nn = NN(cp_layers)
        self.log_tau = Parameter(torch.tensor(0.))
        self.samples = None
        # self.is_train = True

    def get_loss(self, batch_ind, batch_t, batch_y, N):
        batch_size = batch_ind.shape[0]
        pred_mean, pred_var = self.predict_(batch_ind, batch_t)
        loss = 0
        loss -= 0.5 * N * self.log_tau
        # loss += 0.5 * torch.exp(self.log_tau) * ((pred_y - batch_y)**2).sum() / batch_size * N
        loss += 0.5 * torch.exp(self.log_tau) * ((batch_y - pred_mean)**2 + pred_var).sum() / batch_size * N
        loss += 0.5 * (self.ode_func.reg() + self.f.reg())
        # loss += self.ode_func.KL_divergence()
        # loss += self.f.KL_divergence()
        return loss

    def generate_mask(self, ind):
        mask = torch.zeros((self.num_node, self.num_node), device=self.device)
        for i in range(1, self.nmod):
            row = np.sum(self.nvec[:i])
            for j in range(i):
                col = np.sum(self.nvec[:j])
                indij = ind[:, [i, j]]
                indij = torch.unique(indij, dim=0).long()
                row_idx = row + indij[:, 0]
                col_idx = col + indij[:, 1]
                mask[row_idx.long(), col_idx.long()] = 1
        return mask

    # t start from 0
    def train(self, ind, t, y, ind_te, t_te, y_te, batch_size=100, test_every=100, total_epoch=100, lr=1e-3):
        self.to(self.device)
        N = ind.shape[0]
        N_te = ind_te.shape[0]

        t = t.reshape(-1)
        ind = torch.tensor(ind, device=self.device, dtype=torch.int32)
        t = torch.tensor(t, device=self.device)
        y = torch.tensor(y, device=self.device)

        unique_t, t_inverse_index = torch.unique(t, return_inverse=True)
        print(unique_t.shape)
        ind_t = []
        y_t = []
        for i in range(unique_t.shape[0]):
            ind_t.append(ind[t_inverse_index == i])
            y_t.append(y[t_inverse_index == i])

        # set mask of diffusion
        self.ode_func.diffusion.mask = self.generate_mask(ind)

        t_te = t_te.reshape(-1)
        ind_te = torch.tensor(ind_te, device=self.device, dtype=torch.int32)
        t_te = torch.tensor(t_te, device=self.device)
        y_te = torch.tensor(y_te, device=self.device)

        optimizer = Adam(self.parameters(), lr=lr)
        scheduler = ReduceLROnPlateau(optimizer, 'min', min_lr=1e-3, patience=3) 

        idx = np.arange(unique_t.shape[0])
        self.samples = []
        iter_count = 0

        nrmse_list = []
        nmae_list = []
        ll_list = []
        tr_nrmse_list = []
        tr_nmae_list = []
        tr_ll_list = []

        def get_batch(t_idx):
            batch_t = []
            batch_ind = []
            batch_y = []
            for i in t_idx:
                batch_t.append(unique_t[i].repeat(ind_t[i].shape[0]))
                batch_ind.append(ind_t[i])
                batch_y.append(y_t[i])
            batch_t = torch.cat(batch_t)
            batch_ind = torch.cat(batch_ind, 0)
            batch_y = torch.cat(batch_y, 0)
            return batch_t, batch_ind, batch_y

        for epoch in tqdm(range(total_epoch)):
            np.random.shuffle(idx)
            num_batch = (unique_t.shape[0] + batch_size - 1) // batch_size
            for iter in range(num_batch):
                t_idx = idx[iter * batch_size: (iter+1) * batch_size]

                # batch_ind = ind[batch_idx]
                # batch_t = t[batch_idx]
                # batch_y = y[batch_idx]
                batch_t, batch_ind, batch_y = get_batch(t_idx)
                

                optimizer.zero_grad()
                loss = self.get_loss(batch_ind, batch_t, batch_y, N)
                loss.backward()
                # print(self.ode_func[0].W.grad)
                optimizer.step()

                iter_count+=1

            if (epoch + 1) % test_every == 0:
                with torch.no_grad():
                    # testing error
                    nrmse, nmae = self.test(ind_te, t_te, y_te, 2000)
                    nrmse_list.append(nrmse.item())
                    nmae_list.append(nmae.item())
                    # ll_list.append(ll.item())
                    print('Epoch: {} NRMSE: {} NMAE: {}'.format(epoch+1, nrmse, nmae))

                    # training error
                    nrmse, nmae = self.test(ind, t, y, 2000)
                    tr_nrmse_list.append(nrmse.item())
                    tr_nmae_list.append(nmae.item())
                    # tr_ll_list.append(ll.item())
                    print('Training: NRMSE: {} NMAE: {}'.format(nrmse, nmae))
                    scheduler.step(nrmse.item())
                    cur_lr = [group['lr'] for group in optimizer.param_groups][0]
                    print('Current LR:', cur_lr)


        return tr_nrmse_list, tr_nmae_list, tr_ll_list, nrmse_list, nmae_list, ll_list
    
    def test(self, ind, t, y, test_batch_size):
        N = ind.shape[0]
        num_batch = (N + test_batch_size - 1) // test_batch_size
        m_list = []
        var_list = []
        for i in range(num_batch):
            batch_ind = ind[i * test_batch_size: (i+1)*test_batch_size]
            batch_t = t[i * test_batch_size: (i+1)*test_batch_size]
            # batch_y = y[i * test_batch_size: (i+1)*test_batch_size]
            pred_m, pred_var = self.predict_(batch_ind, batch_t, 'dopri5')
            m_list.append(pred_m)
            var_list.append(pred_var)
            # print(pred_y)
            # se += ((batch_y - pred_y)**2).sum()
            # ae += torch.abs(batch_y - pred_y).sum()
        if len(m_list) > 1:
            pred_m = torch.cat(m_list)
            # pred_var = torch.cat(var_list)
        else:
            pred_m = m_list[0]
            # pred_var = var_list[0]

        # sigma2 = torch.exp(-self.log_tau) + pred_var
        # ll = -0.5 / sigma2 * (pred_m - y)**2  - 0.5 * torch.log(sigma2) - 0.5 * np.log(2 * np.pi)
        # ll = ll.sum()
        # pred_y = pred_y.mean(dim=1).view((-1, 1))
        nrmse = torch.sqrt(((pred_m - y)**2).mean()) / torch.sqrt((y**2).mean())
        nmae = torch.abs(pred_m - y).mean() / torch.abs(y).mean()

        return nrmse, nmae#, ll

    # # for all sampled paramters
    # def predict(self, batch_ind, batch_t, last=1):
    #     y_list = []
    #     for sample in self.samples[-last:]:
    #         self.model_parameters = sample
    #         pred_y = self.predict_(batch_ind, batch_t)
    #         y_list.append(pred_y)
    #     pred_y = torch.cat(y_list, 1)
    #     return pred_y


    # for current parameters
    def predict_(self, batch_ind, batch_t, method='dopri5'):
        batch_size = batch_ind.shape[0]
        unique_t, inverse_indices = torch.unique(batch_t, sorted=True, return_inverse=True) 
        if unique_t[0] > 0:
            t_points = torch.cat([torch.tensor([0.], device=self.device), unique_t])
        else:
            t_points = unique_t
        e = odeint(self.ode_func, self.IC.view(-1), t_points, method=method)#, method='rk4')
        e = e.view((e.shape[0], -1))
        if unique_t.shape[0] < t_points.shape[0]:
            e = e[1:]
        e = e[inverse_indices].view((-1, self.num_node, self.dim_embedding)) 
        e = torch.split(e, self.nvec, dim=1)
        # CP
        # embedding_prod = torch.ones((batch_size, self.dim_embedding)).to(self.device)
        embeddings = []
        # idx = torch.tensor(np.cumsum(self.nvec) - self.nvec[0]).view((1, -1)).to(self.device) + batch_ind
        # print(e.shape)
        # embedding = e[np.arange(batch_size).astype(np.int64), idx.long()]
        # print(embedding.shape)
        for k in range(self.nmod):
            ek = e[k]
            idx = batch_ind[:, k].long()
            # embedding_prod *= e[np.arange(batch_size).astype(np.int64), idx].view((batch_size, self.dim_embedding))
            embeddings.append(ek[np.arange(batch_size).astype(np.int64), idx].view((batch_size, self.dim_embedding)))
        embeddings = torch.cat(embeddings, 1)
        # if self.is_train:
            # pred_y = self.f(embeddings)
        # else:
            # pred_y = self.f.forward_mean(embeddings)
        # pred_mean, pred_var = self.f(embeddings)
        pred_mean = self.f(embeddings)
        pred_var = 0
        # pred_y = embedding_prod.sum(1).view(-1, 1)
        return pred_mean, pred_var

                





