import os
import shutil
import warnings
import argparse
import random

import pandas as pd
import numpy as np
import multiprocessing as mp

from datetime import datetime
from Bio import BiopythonWarning
from Bio.PDB import PDBParser
from Bio.PDB.PDBIO import PDBIO
from Bio.PDB.Model import Model
from Bio.PDB.Residue import Residue
from Bio.PDB.Chain import Chain
from dockq.DockQ import calc_DockQ
warnings.simplefilter('ignore', FutureWarning)
warnings.simplefilter('ignore', BiopythonWarning)


THREE_TO_ONE ={'VAL':'V', 'ILE':'I', 'LEU':'L', 'GLU':'E', 'GLN':'Q', \
    'ASP':'D', 'ASN':'N', 'HIS':'H', 'TRP':'W', 'PHE':'F', 'TYR':'Y', \
    'ARG':'R', 'LYS':'K', 'SER':'S', 'THR':'T', 'MET':'M', 'ALA':'A', \
    'GLY':'G', 'PRO':'P', 'CYS':'C', 'UNK': 'X', 'SEC': 'U', 'PYL': 'O'} # SEC 硒代半胱氨酸， PYL 吡咯赖氨酸

def get_seq(obj):
    seq = ''
    for i in obj.get_residues():
        if 'CA' in i:
            seq += THREE_TO_ONE.get(i.resname, 'X')
        else:
            seq += 'X'

    return seq


def parse_chain(chain):
    seq = ''
    cals = []
    heavls = []
    last_res_idx = 0
    gap_positions = []
    for res in chain.get_residues():
        if res.id[0] != ' ':
            continue
        
        res_idx = int(res.id[1])
        
        delta_idx = res_idx - last_res_idx
        gap_positions.extend(list(range(last_res_idx + 1, res_idx)))
        if delta_idx != 1:
            seq += 'X' * (delta_idx - 1)
            for _ in range(delta_idx - 1):
                cals.append([0, 0, 0])
        last_res_idx = res_idx
        
        resname = THREE_TO_ONE[res.resname] if 'CA' in res else 'X'
        seq += resname
        if resname == 'X':
            cals.append([0, 0, 0])
        else:
            for at in res.get_atoms():
                if at.id == 'CA':
                    cals.append(at.coord)
                elif at.element != 'H':
                    heavls.append(at.coord)

    ca_pos = np.array(cals)
    heav_pos = np.array(heavls)
    # print(ca_pos.shape)
    assert len(seq) == ca_pos.shape[0], (len(seq), ca_pos.shape)
    mask = np.array([i != 'X' for i in seq], dtype=bool)

    # insert UNK residues at gap positions
    for gap_position in gap_positions:
        unk_residue = Residue((' ', gap_position, ' '), 'UNK', 0)
        chain.insert(gap_position-1, unk_residue) # pos is 0-indexed
    assert get_seq(chain) == seq, f'{get_seq(chain)}\n{seq}\n'
    return seq, ca_pos, mask, heav_pos


def cal_rmsd(x1, x2, eps = 1e-6):
    assert x1.shape == x2.shape, (x1.shape, x2.shape)
    assert x1.shape[-1] == 3
    return np.sqrt(((x1 - x2) ** 2).sum(-1).mean() + eps)

def kabsch_rmsd(true_atom_pos, pred_atom_pos):
    r, x = get_optimal_transform(
        true_atom_pos,
        pred_atom_pos
    )
    aligned_true_atom_pos = true_atom_pos @ r + x
    return cal_rmsd(aligned_true_atom_pos, pred_atom_pos)


def cal_ca_kabsch_rmsd(pred_ca, truth_ca, truth_cids, pm):
    # truth_ca[chain_id] = [ca_pos, mask, heav_pos]
    # pred_ca[chain.id] = ca_pos
    pred_ca_ls = []
    truth_ca_ls = []
    for truth_cid, pred_idx in zip(truth_cids, pm):
        truth_ca_pos, truth_mask, _ =truth_ca[truth_cid]
        pred_ca_pos = list(pred_ca.values())[pred_idx]
        truth_ca_ls.append(truth_ca_pos[truth_mask])
        pred_ca_ls.append(pred_ca_pos[np.pad(truth_mask, (0, pred_ca_pos.shape[0]-len(truth_mask)))])
        truth_ca_all = np.concatenate(truth_ca_ls)
        pred_ca_all = np.concatenate(pred_ca_ls)
    return kabsch_rmsd(truth_ca_all, pred_ca_all)

def get_optimal_transform(src_atoms, tgt_atoms, mask = None):    
    assert src_atoms.shape == tgt_atoms.shape, (src_atoms.shape, tgt_atoms.shape)
    assert src_atoms.shape[-1] == 3
    if mask is not None:
        assert mask.dtype == bool
        assert mask.shape[-1] == src_atoms.shape[-2]
        if mask.sum() == 0:
            src_atoms = np.zeros((1, 3)).astype(np.float32)
            tgt_atoms = src_atoms
        else:
            src_atoms = src_atoms[mask, :]
            tgt_atoms = tgt_atoms[mask, :]
    src_center = src_atoms.mean(-2, keepdims=True)
    tgt_center = tgt_atoms.mean(-2, keepdims=True)

    r = kabsch_rotation(src_atoms - src_center, tgt_atoms - tgt_center)
    x = tgt_center - src_center @ r
    return r, x


def kabsch_rotation(P, Q):
    """
    Using the Kabsch algorithm with two sets of paired point P and Q, centered
    around the centroid. Each vector set is represented as an NxD
    matrix, where D is the the dimension of the space.
    The algorithm works in three steps:
    - a centroid translation of P and Q (assumed done before this function
      call)
    - the computation of a covariance matrix C
    - computation of the optimal rotation matrix U
    For more info see http://en.wikipedia.org/wiki/Kabsch_algorithm
    Parameters
    ----------
    P : array
        (N,D) matrix, where N is points and D is dimension.
    Q : array
        (N,D) matrix, where N is points and D is dimension.
    Returns
    -------
    U : matrix
        Rotation matrix (D,D)
    """

    # Computation of the covariance matrix
    C = P.transpose(-1, -2) @ Q
    # Computation of the optimal rotation matrix
    # This can be done using singular value decomposition (SVD)
    # Getting the sign of the det(V)*(W) to decide
    # whether we need to correct our rotation matrix to ensure a
    # right-handed coordinate system.
    # And finally calculating the optimal rotation matrix U
    # see http://en.wikipedia.org/wiki/Kabsch_algorithm
    V, _, W = np.linalg.svd(C)
    d = (np.linalg.det(V) * np.linalg.det(W)) < 0.0

    if d:
        V[:, -1] = -V[:, -1]

    # Create Rotation matrix U
    U = V @ W
    return U


def get_mean_pred(pred_ca_pos, mask, pred_cid, truth_cid, df):
    assert pred_ca_pos.shape[-1] == 3
    if pred_cid not in df.pred_cid[df.truth_cid == truth_cid].values[0]:
        return np.array([1e9, 1e9, 1e9]).reshape(1, -1)
    else:
        mask = np.pad(mask, (0, pred_ca_pos.shape[0]-len(mask))) # pad mask to pred residue length
        return pred_ca_pos[mask].mean(0, keepdims=True)


def find_optimal_permutation(x_mean_pred, x_mean_truth):
    '''Find Optimal Permutation'''
    assert x_mean_pred.shape[-1] == 3, x_mean_pred.shape
    assert x_mean_truth.shape[-1] == 3, x_mean_truth.shape
    d_kl = np.sqrt(((x_mean_pred - x_mean_truth[None]) ** 2).sum(-1))
    p_l = []
    for l in range(d_kl.shape[1]):
        d_l = d_kl[:, l]
        best_idx = d_l.argmin()
        p_l.append(best_idx)
        d_kl[best_idx, :] = 1e9
    return p_l

def has_contact(chain1, chain2):
    '''defined as any heavy atom of one chain being within 5A of any heavy atom of the other chain'''
    d = np.expand_dims(chain1, 1) - chain2[None, :]
    dist = np.sqrt((d ** 2).sum(-1))
    return (dist <= 5).any()


def rm_masked_res(chain, mask):
    chain1 = Chain(chain.id)
    for res, m in zip(chain.child_list, mask):
        if m:
            chain1.add(res)
    return chain1

def show_model(model):
    return [len(list(chain)) for chain in model.get_chains()]


def cal_dockq_pdb(pred_pdb_path, truth_pdb_path, pdb_id=None, key=None, save_mode=False):

    timestr = datetime.now().strftime('%Y%m%d%H%M%S')
    if key is None:
        key = f'{timestr}_{random.randint(0, 1e4):0>5}'
    else:
        key = f'{key}_{timestr}_{random.randint(0, 1e4):0>5}'
    
    if pdb_id is None:
        if os.path.isdir(truth_pdb_path):
            raise ValueError('"pdb_id" should be provided when "truth_pdb_path" is a directory.')
        else:
            pdb_id = os.path.basename(truth_pdb_path).split('.')[0]
    tmp_dir = f'_tmp/{pdb_id}_{key}'
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        parser = PDBParser(QUIET=True)
        chain_dict = {}
        pred_ca = {}
        pred = parser.get_structure('pred', pred_pdb_path)[0]
        for chain in pred.get_chains():
            seq, ca_pos, _, _ = parse_chain(chain)
            pred_ca[chain.id] = ca_pos

            if seq in chain_dict:
                chain_dict[seq].append(chain.id)
            else:
                chain_dict[seq] = [chain.id]
            
        ls = []
        for k, v in chain_dict.items():
            ls.append([''.join(v), len(k), k])
        df_pred = pd.DataFrame(ls, columns=['pred_cid', 'seq_len', 'pred_seq'])
        df_pred['num_chains'] = df_pred.pred_cid.map(len)

        # ground truth pdb
        # merge and rename all chains in gt
        ls = []
        truth_ca = {}
        truth_chain = {}
        PDB_CHAIN_IDS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789' # 62

        # get truth pdb chains list
        if os.path.isfile(truth_pdb_path):
            truth_pdbs = parser.get_structure('truth', truth_pdb_path)[0].child_list
        elif os.path.isdir(truth_pdb_path):
            if pdb_id is None:
                raise ValueError('"pdb_id" should be provided when "truth_pdb_path" is a directory.')
            truth_pdbs = [i.strip() for i in os.popen(f'find {truth_pdb_path} -name {pdb_id}*pdb').readlines()]
            truth_pdbs = [i for i in truth_pdbs if not os.path.samefile(i, pred_pdb_path)]
            truth_pdbs = [parser.get_structure('truth', i)[0].child_list[0] for i in truth_pdbs]
        assert len(truth_pdbs) == len(pred.child_list), f'The number of ground truth chains is not equal to that of prediction: {len(truth_pdbs), len(pred.child_list)}'
        
        for i, chain in enumerate(truth_pdbs):
            truth_pdb =chain.id
            chain_id = PDB_CHAIN_IDS[i]
            truth_chain[chain_id] = chain
            seq, ca_pos, mask, heav_pos = parse_chain(chain)
            truth_ca[chain_id] = [ca_pos, mask, heav_pos]

            for i, row in df_pred.iterrows():
                if row.seq_len < len(seq):
                    continue
                flag_match = True
                for j, k in zip(row.pred_seq, seq):
                    if not (j == k or k == 'X' or j == 'X'):
                        flag_match = False
                        break
                if flag_match:
                    ls.append([*row, chain_id, truth_pdb, seq, mask])
        df = pd.DataFrame(ls, columns=df_pred.columns.tolist() + ['truth_cid', 'truth_path', 'truth_seq', 'mask']) 
        df['true_seq_len'] = df['mask'].map(np.sum)
        df = df.sort_values(by=['num_chains', 'true_seq_len'], ascending=[True, False])
        print(df.to_string())
        df = df[['pred_cid', 'seq_len', 'num_chains', 'truth_cid', 'true_seq_len', 'truth_path', 'pred_seq', 'truth_seq']]
        if save_mode:
            df.to_csv(f'{tmp_dir}/{pdb_id}_info.tsv', sep='\t', index=False)

        truth_cids = df.truth_cid
        anchor_truth = truth_cids[0]
        
        anchors_pred = list(df.pred_cid[0])
        masks = [truth_ca[i][1] for i in truth_cids]
        # x_mean_pred: (num_pred_chain, num_truth_chain, 3)
        x_mean_pred = np.concatenate([np.concatenate([get_mean_pred(pred_ca_pos, mask, pred_cid, truth_cid, df) for pred_cid, pred_ca_pos in pred_ca.items()])[:, None] for truth_cid, mask in zip(truth_cids, masks)], 1)

        # print(x_mean_pred.shape)
        pm_best = []
        rmsd_min = 1e9
        for anchor_pred in anchors_pred:
            ca_t, mask, _ = truth_ca[anchor_truth]
            ca_p = pred_ca[anchor_pred][:len(mask)]
            r, t = get_optimal_transform(ca_t, ca_p, mask)
            x_mean_truth = np.concatenate([(truth_ca[i][0][truth_ca[i][1]] @ r + t).mean(0, keepdims=True) for i in truth_cids])
            # print(x_mean_truth.shape)
            pm = find_optimal_permutation(x_mean_pred, x_mean_truth)
            # rmsd = cal_rmsd(x_mean_truth, x_mean_pred[pm, range(len(pm))])
            rmsd = cal_ca_kabsch_rmsd(pred_ca, truth_ca, truth_cids, pm)
            print([anchor_truth, anchor_pred, pm, rmsd])
            if rmsd < rmsd_min:
                rmsd_min = rmsd
                pm_best = pm


        match_table = {}
        for cid_t, cid_p in zip(truth_cids, np.array(list(pred_ca.keys()))[pm_best]):
            cids_p = df.pred_cid[df.truth_cid == cid_t].values[0]
            assert cid_p in cids_p, (cid_p, cids_p)
            match_table[cid_t] = cid_p
        if save_mode:
            with open(f'{tmp_dir}/{pdb_id}_match_table.tsv', 'w') as f:
                f.writelines([f'{df.truth_path[df.truth_cid == k].values[0]}\t{v}\n' for k, v in match_table.items()])


        n_chains = len(match_table)
        dockqls = []
        for i in range(n_chains - 1):
            for j in range(i + 1, n_chains):
                cid_ti = list(match_table.keys())[i]
                cid_tj = list(match_table.keys())[j]
                cont = has_contact(truth_ca[cid_ti][2], truth_ca[cid_tj][2])
                if not cont:
                    continue
                cid_pi = match_table[cid_ti]
                cid_pj = match_table[cid_tj]
                file_pred = f'{tmp_dir}/pred_{cid_pi}_{cid_pj}.pdb'
                file_truth = f'{tmp_dir}/truth_{cid_pi}_{cid_pj}.pdb'
                mask_i = truth_ca[cid_ti][1]
                mask_j = truth_ca[cid_tj][1]
                io = PDBIO()
                model_p = Model(0)
                model_p.add(rm_masked_res(pred.child_dict[cid_pi], mask_i))
                model_p.add(rm_masked_res(pred.child_dict[cid_pj], mask_j))
                io.set_structure(model_p)
                io.save(file_pred)
                model_t = Model(0)
                chain_i = truth_chain[cid_ti].copy()
                chain_i.id = cid_pi
                chain_j = truth_chain[cid_tj].copy()
                chain_j.id = cid_pj
                model_t.add(rm_masked_res(chain_i, mask_i))
                model_t.add(rm_masked_res(chain_j, mask_j))
                io.set_structure(model_t)
                io.save(file_truth)
                print(f'seq len for chain {cid_ti} and {cid_tj}: {show_model(model_t)} (truth), {show_model(model_p)} (pred)')
                assert get_seq(model_t) == get_seq(model_p), f'\n{get_seq(model_t)}\n{get_seq(model_p)}\n'
                info = calc_DockQ(file_pred, file_truth)
                info['pdb_id'] = pdb_id
                info['pred_i'] = cid_pi
                info['pred_j'] = cid_pj
                info['truth_i'] = df.truth_path[df.truth_cid == cid_ti].values[0]
                info['truth_j'] = df.truth_path[df.truth_cid == cid_tj].values[0]
                dockqls.append(pd.DataFrame(info, index=[0]))
        
        if len(dockqls) == 0:
            print(f'No contact: {pdb_id}')
            return None
        dockqdf = pd.concat(dockqls, axis=0)
        cols = ['pdb_id', 'pred_i', 'pred_j', 'DockQ', 'irms', 'Lrms', 'fnat', 'nat_correct', 'nat_total', 'fnonnat', 'nonnat_count', 'model_total', 'chain1', 'chain2', 'len1', 'len2', 'class1', 'class2', 'truth_i', 'truth_j']
        dockqdf = dockqdf[cols]
        print(dockqdf.to_string())
        if save_mode:
            dockqdf.to_csv(f'{tmp_dir}/{pdb_id}_dockq_info.tsv', sep='\t', index=False)
        dockq = dockqdf.DockQ.mean()
        dockq = round(dockq, 5)
        rmsd = round(rmsd, 5)
        return dockq, rmsd
    # except:
    #     return None, None
    finally:
        if not save_mode:
            shutil.rmtree(tmp_dir) 


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Calculate DockQ for protein complex')
    parser.add_argument('pred_pdb_path', type=str, help='a pdb file containing predicted structures of all chains')
    parser.add_argument('truth_pdb_path', type=str, help='a directory containing all ground truth pdb files, with each file corresponding to one chain, or it can also be a pdb file consisting of all chains')
    parser.add_argument('--pdb_id', type=str, default=None, help='PDB id, if "truth_pdb_path" is a directory, all files in "truth_pdb_path" with the pattern "pdb_id***pdb" (excluding pred_pdb_path) will be recognized as ground truth pdbs')
    parser.add_argument('--key', type=str,default=None, help='output directory identifier')
    parser.add_argument('--save_mode', action='store_true', help='it will save intermediate results with this mode on')
    args = parser.parse_args()
    

    dockq, rmsd = cal_dockq_pdb(args.pred_pdb_path, args.truth_pdb_path, args.pdb_id, args.key, args.save_mode)
    print(f'RMSD: {rmsd}\nAveraged DockQ: {dockq}')