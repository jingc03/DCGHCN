#%%
import torch
from torch import nn
import torch.nn.functional as F
from torch_geometric.nn import GraphConv
from torch_geometric.nn import GraphSAGE
from torch_geometric.nn import GCN
from torch_geometric.nn import GAT

import math
import numpy as np
import pandas as pd

import matplotlib.pyplot as plt
from sklearn.metrics import roc_curve, auc
from sklearn.metrics import precision_recall_curve

from copy import deepcopy
from tqdm import tqdm
from sklearn.model_selection import KFold
from sklearn.metrics import roc_auc_score, average_precision_score

import os
os.chdir(os.path.realpath(os.path.join(__file__, '..')))
from prepareData import prepare_data

np.random.seed(123)
torch.manual_seed(123)
torch.cuda.manual_seed(123)
torch.cuda.manual_seed_all(123)
torch.backends.cudnn.deterministic = True
device = 'cuda' if torch.cuda.is_available() else 'cpu'

#%%
class Config(object):
    def __init__(self):
        self.data_path = './datasets/dataset1'
        self.nfold = 5
        self.n_rep = 10 
        self.k_neig = 5
        self.emb_dim = 64
        self.hid_dim = 64
        self.dropout = 0
        self.num_epoches = 1000

def calculate_evaluation_metrics(pred_mat, pos_edges, neg_edges):
    pos_pred_socres = pred_mat[pos_edges[0], pos_edges[1]]
    neg_pred_socres = pred_mat[neg_edges[0], neg_edges[1]]
    pred_scores = np.hstack((pos_pred_socres, neg_pred_socres))
    true_labels = np.hstack((np.ones(pos_pred_socres.shape[0]), np.zeros(neg_pred_socres.shape[0])))

    auc = roc_auc_score(true_labels, pred_scores)
    average_precision = average_precision_score(true_labels, pred_scores)

    pred_scores_mat = np.mat([pred_scores])
    true_labels_mat = np.mat([true_labels])
    sorted_predict_score = np.array(sorted(list(set(np.array(pred_scores_mat).flatten()))))
    sorted_predict_score_num = len(sorted_predict_score)
    thresholds = sorted_predict_score[
        (np.array([sorted_predict_score_num]) * np.arange(1, 1000) / np.array([1000])).astype(int)]
    thresholds = np.mat(thresholds)
    thresholds_num = thresholds.shape[1]

    predict_score_matrix = np.tile(pred_scores_mat, (thresholds_num, 1))
    negative_index = np.where(predict_score_matrix < thresholds.T)
    positive_index = np.where(predict_score_matrix >= thresholds.T)
    predict_score_matrix[negative_index] = 0
    predict_score_matrix[positive_index] = 1

    TP = predict_score_matrix * true_labels_mat.T
    FP = predict_score_matrix.sum(axis=1) - TP
    FN = true_labels_mat.sum() - TP
    TN = len(true_labels_mat.T) - TP - FP - FN
    tpr = TP / (TP + FN)
    f1_score_list = 2 * TP / (len(true_labels_mat.T) + TP - TN)
    accuracy_list = (TP + TN) / len(true_labels_mat.T)

    max_index = np.argmax(f1_score_list)
    f1_score = f1_score_list[max_index, 0]
    accuracy = accuracy_list[max_index, 0]
    return np.array([auc, average_precision, f1_score, accuracy])

def impute_zeros(inMat,inSim,k=10):
	mat = deepcopy(inMat)
	sim = deepcopy(inSim)
	(row,col) = mat.shape
 	# np.fill_diagonal(mat,0)
	indexZero = np.where(~mat.any(axis=1))[0]
	numIndexZeros = len(indexZero)

	np.fill_diagonal(sim,0)
	if numIndexZeros > 0:
		sim[:,indexZero] = 0
	for i in indexZero:
		currSimForZeros = sim[i,:]
		indexRank = np.argsort(currSimForZeros)

		indexNeig = indexRank[-k:]
		simCurr = currSimForZeros[indexNeig]

		mat_known = mat[indexNeig, :]
		
		if sum(simCurr) >0:  
			mat[i,: ] = np.dot(simCurr ,mat_known) / sum(simCurr)
	return mat


def generate_G_from_H(H):
    DV = np.sum(H, axis=1)
    DE = np.sum(H, axis=0)
    DV[DV==0] = np.inf
    DE[DE==0] = np.inf
    invDE = np.mat(np.diag(np.power(DE, -1)))
    DV2 = np.mat(np.diag(np.power(DV, -0.5)))
    H = np.mat(H)
    HT = H.T
    G = DV2 @ H @ invDE @ HT @ DV2
    return G


class HGNN_conv(nn.Module):
    def __init__(self, in_ft, out_ft, bias=True):
        super(HGNN_conv, self).__init__()

        self.weight = nn.Parameter(torch.Tensor(in_ft, out_ft))
        if bias:
            self.bias = nn.Parameter(torch.Tensor(out_ft))
        else:
            self.register_parameter('bias', None)
        self.reset_parameters()

    def reset_parameters(self):
        stdv = 1. / math.sqrt(self.weight.size(1))
        self.weight.data.uniform_(-stdv, stdv)
        if self.bias is not None:
            self.bias.data.uniform_(-stdv, stdv)

    def forward(self, x: torch.Tensor, G: torch.Tensor):
        z = G.matmul(x) + x
        return z


class HGNN_embedding(nn.Module):
    def __init__(self, in_ch, n_hid, dropout=0.1):
        super(HGNN_embedding, self).__init__()
        self.dropout = dropout
        self.hgc1 = HGNN_conv(in_ch, n_hid)

    def forward(self, x, G):
        x = F.dropout(x, self.dropout)
        x = self.hgc1(x, G)
        return x


class Net(nn.Module):
    def __init__(self, opt, num_users, num_items):
        super(Net, self).__init__()
        self.dropout = opt.dropout
        self.user_emb = nn.Parameter(nn.init.xavier_normal_(torch.empty(num_users, opt.emb_dim)))
        self.item_emb = nn.Parameter(nn.init.xavier_normal_(torch.empty(num_items, opt.emb_dim)))
        self.user_encoder_s = GraphSAGE(opt.emb_dim, opt.hid_dim, num_layers=1)
        self.item_encoder_s = GraphSAGE(opt.emb_dim, opt.hid_dim, num_layers=1)
        self.user_encoder = HGNN_embedding(opt.hid_dim, opt.hid_dim, 0)
        self.item_encoder = HGNN_embedding(opt.hid_dim, opt.hid_dim, 0)

    def forward(self, Gs, Gh):
        user_Gs = Gs['user']
        item_Gs = Gs['item']
        user_Gh = Gh['user']
        item_Gh = Gh['item']
        
        user_x = F.dropout(self.user_emb, 0)
        item_x = F.dropout(self.item_emb, 0)
        
        user_z1 = self.user_encoder_s(user_x, user_Gs)
        item_z1 = self.item_encoder_s(item_x, item_Gs)
        
        user_z1 = F.dropout(user_z1, self.dropout)
        item_z1 = F.dropout(item_z1, self.dropout)
	    
        user_z = self.user_encoder(user_z1, user_Gh)
        item_z = self.item_encoder(item_z1, item_Gh)
        user_z = self.user_encoder(user_z, user_Gh)
        item_z = self.item_encoder(item_z, item_Gh)
        pred_ratings = torch.mm(user_z, item_z.t())
        return pred_ratings

#%%
opt = Config()

dataset = prepare_data(opt)
num_users, num_items = dataset['md_p'].shape

metric_tab = np.zeros((opt.nfold, opt.n_rep, 4))
auc_scores = []

for ir in range(opt.n_rep):
    kf = KFold(n_splits = opt.nfold, shuffle = True)
    for ik, (train, test) in enumerate(kf.split(range(num_users))):
        Htrain = dataset['md_p'].copy() 
        Htrain[test,] = 0
        Htrain = impute_zeros(Htrain, dataset['mm']['data'])
        
        Hm = np.minimum(np.c_[Htrain, Htrain@(Htrain.T@Htrain)], 1)
        Hd = np.minimum(np.c_[Htrain.T, Htrain.T@(Htrain@Htrain.T)], 1)
        
        Gm = generate_G_from_H(Hm)
        Gd = generate_G_from_H(Hd)
        
        Htrain = torch.tensor(Htrain, dtype = torch.float).to(device)
        Gh = {'user':torch.tensor(Gm, dtype = torch.float).to(device), 'item':torch.tensor(Gd, dtype = torch.float).to(device)}
        model = Net(opt, num_users, num_items).to(device)
        model.train()
        optimizer = torch.optim.Adam(model.parameters(), lr = 1e-3, weight_decay=5e-4)
        for epoch in tqdm(range(opt.num_epoches)):
            scores = model(Gs, Gh)
            loss = F.binary_cross_entropy_with_logits(scores, Htrain)
            # loss = (1 - opt.alpha)*loss_sum[train,].sum() + opt.alpha*loss_sum[test,].sum()
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        
        model.eval()
        y_score = model(Gs, Gh).detach().cpu().numpy()
        test = test[dataset['md_p'][test,].sum(1)!=0]
        metrics = calculate_evaluation_metrics(y_score[test,], np.where(dataset['md_p'][test,]==1), np.where(dataset['md_p'][test,]==0))
        metric_tab[ik,ir,] = metrics

#%%
np.savez(r'---' )
metric_columns = ['AUC', 'Average Precision', 'F1 Score', 'Accuracy']
metric_df = pd.DataFrame(metric_tab.reshape(-1, 4), columns=metric_columns)
metric_df.to_excel(r'---', index=False)

column_names = [f'Predicted Rating {i+1}' for i in range(y_score.shape[1])]
pos_pred_socres = y_score[test,][np.where(dataset['md_p'][test,]==1)[0], np.where(dataset['md_p'][test,]==1)[1]]
neg_pred_socres = y_score[test,][np.where(dataset['md_p'][test,]==0)[0], np.where(dataset['md_p'][test,]==0)[1]]
pred_scores = np.hstack((pos_pred_socres, neg_pred_socres))
true_labels = np.hstack((np.ones(pos_pred_socres.shape[0]), np.zeros(neg_pred_socres.shape[0])))
prediction_df = pd.DataFrame(y_score, columns=column_names)
prediction_df.to_excel('predicted_ratings.xlsx', index=False)
prediction_df.to_excel(r'---', index=False)
for i in range(opt.nfold):
    fpr, tpr, _ = roc_curve(true_labels, pred_scores)
    precision1, recall1, _ = precision_recall_curve(true_labels, pred_scores)
    aupr = auc(recall1, precision1)  # the value of roc_auc1
    plt.plot(recall1, precision1, 'b', label='AUC = %0.4f' % aupr)

    roc_auc = auc(fpr, tpr)
    auc_scores.append(roc_auc)

    plt.figure()
    plt.plot(fpr, tpr, color='red', lw=2, label=f'DCGHCN (AUC = {roc_auc:.4f})')
    plt.plot([0, 1], [0, 1], color='navy', lw=2, linestyle='--')
    plt.xlim([0.0, 1.0])
    plt.ylim([0.0, 1.05])
    plt.xlabel('False Positive Rate')
    plt.ylabel('True Positive Rate')
    plt.title('Receiver Operating Characteristic Curve')
    plt.legend(loc='lower right')
    plt.savefig(f'roc_curve_fold_{i+1}.jpg')
    plt.close()

    aupr_data = np.column_stack((recall1, precision1))
    np.savetxt(f'aupr_curve_fold_{i+1}GraphSAGE l=3.csv', aupr_data, delimiter=',', header='Recall,precision', comments='')
    roc_data = np.column_stack((fpr, tpr))
    np.savetxt(f'roc_curve_fold_{i+1}GraphSAGE l=3.csv', roc_data, delimiter=',', header='False Positive Rate,True Positive Rate', comments='')

auc_df = pd.DataFrame({'AUC': auc_scores})
auc_df.to_excel(r'---', index=False)