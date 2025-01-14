import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

#from core.modules.admm import ADMMSolverBlock
from core.modules.admm_v1_1 import ADMMSolverBlock
from core.modules.update import BasicUpdateBlock, SmallUpdateBlock
from core.modules.extractor import BasicEncoder, SmallEncoder
from core.modules.corr import CorrBlock
from core.utils.utils import bilinear_sampler, coords_grid, upflow8

class RAFT(nn.Module):
    def __init__(self, args):
        super(RAFT, self).__init__()
        self.args = args

        if args.small:
            self.hidden_dim = hdim = 96
            self.context_dim = cdim = 64
            args.corr_levels = 4
            args.corr_radius = 3
        
        else:
            self.hidden_dim = hdim = 128
            self.context_dim = cdim = 128
            args.corr_levels = 4
            args.corr_radius = 4

        if 'dropout' not in args._get_kwargs():
            args.dropout = 0

        # feature network, context network, and update block
        if args.small:
            self.fnet = SmallEncoder(output_dim=128, norm_fn='instance', dropout=args.dropout)        
            self.cnet = SmallEncoder(output_dim=hdim+cdim, norm_fn='none', dropout=args.dropout)
            self.update_block = SmallUpdateBlock(self.args, hidden_dim=hdim)

        else:
            self.fnet = BasicEncoder(output_dim=256, norm_fn='instance', dropout=args.dropout)        
            self.cnet = BasicEncoder(output_dim=hdim+cdim, norm_fn='batch', dropout=args.dropout)
            self.update_block = BasicUpdateBlock(self.args, hidden_dim=hdim)
        
        if args.admm_solver:
            #self.admm_block = ADMMSolverBlock(shape=[sh // 8 for sh in args.image_size]+[int(np.ceil(args.batch_size/torch.cuda.device_count()))], 
            #    mask=args.admm_mask, rho=args.admm_rho, lamb=args.admm_lamb, eta=args.admm_eta, T=args.admm_iters)
            self.admm_block = ADMMSolverBlock(mask=args.admm_mask, rho=args.admm_rho, 
                lamb=args.admm_lamb, learn_lamb=args.learn_lamb, eta=args.admm_eta, learn_eta=args.learn_eta, T=args.admm_iters)

    def freeze_bn(self):
        for m in self.modules():
            if isinstance(m, nn.BatchNorm2d):
                m.eval()

    def initialize_flow(self, img):
        """ Flow is represented as difference between two coordinate grids flow = coords1 - coords0"""
        N, C, H, W = img.shape
        coords0 = coords_grid(N, H//8, W//8).to(img.device)
        coords1 = coords_grid(N, H//8, W//8).to(img.device)

        # optical flow computed as difference: flow = coords1 - coords0
        return coords0, coords1

    def forward(self, image1, image2, iters=12, flow_init=None, upsample=True):
        """ Estimate optical flow between pair of frames """

        image1 = 2 * (image1 / 255.0) - 1.0
        image2 = 2 * (image2 / 255.0) - 1.0

        hdim = self.hidden_dim
        cdim = self.context_dim

        # run the feature network
        fmap1, fmap2 = self.fnet([image1, image2])
        corr_fn = CorrBlock(fmap1, fmap2, radius=self.args.corr_radius)

        # run the context network
        cnet = self.cnet(image1)
        net, inp = torch.split(cnet, [hdim, cdim], dim=1)
        net, inp = torch.tanh(net), torch.relu(inp)

        # if dropout is being used reset mask
        self.update_block.reset_mask(net, inp)
        coords0, coords1 = self.initialize_flow(image1)

        flow_predictions = []
        dlta_flows = []
        q = []
        c = []
        betas = []
        for itr in range(iters):
            coords1 = coords1.detach()
            corr = corr_fn(coords1) # index correlation volume

            flow = coords1 - coords0
            net, delta_flow = self.update_block(net, inp, corr, flow)

            # F(t+1) = F(t) + \Delta(t)
            coords1 = coords1 + delta_flow

            # Apply ADMM Solver
            F = coords1 - coords0

            if upsample:
                flow_predictions.append(upflow8(F))
                dlta_flows.append(upflow8(delta_flow))
            else:
                flow_predictions.append(F)
                dlta_flows.append(delta_flow)

            if self.args.admm_solver:
                Q, C, beta = self.admm_block(F, image1)
                q.append(Q)
                c.append(C)
                betas.append(beta)

        return flow_predictions,(q,c,betas),dlta_flows


