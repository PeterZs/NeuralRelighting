import os
import json
import glob
import torch
import itertools
import numpy as np
import torch.nn as nn
import torch.optim as optim
import torchvision.utils as vutils

from time import time
from models import network, render
from torch.optim import lr_scheduler


class Model():
    """Build model"""
    def __init__(self, opts=None):
        """init"""
        self.opts  = opts
        self.name  = opts.name
        self.train = opts.train
        self.aux   = True
        self.aux_cnt = 1
        # build model
        self.build_net()
        # save options
        self.save_opt()

    def save_opt(self):
        """save training options to file"""
        path = '%s/%s' % (self.opts.outf, self.opts.name)
        if not os.path.exists(path):
            os.makedirs(path)
        with open(os.path.join(path, 'config.json'), 'w') as f:
            json.dump(vars(self.opts), f)

    def build_net(self):
        """Setup generator, optimizer, loss func and transfer to device
        """
        # Build net
        self.encoder = nn.DataParallel(network.encoderInitial(intc=4), device_ids=self.opts.gpu_id).cuda()
        self.decoder_brdf = nn.DataParallel(network.decoderBRDF(), device_ids=self.opts.gpu_id).cuda()
        self.decoder_render = nn.DataParallel(network.decoderRender(litc=30), device_ids=self.opts.gpu_id).cuda()
        self.env_predictor = nn.DataParallel(network.envmapInitial(), device_ids=self.opts.gpu_id).cuda()

        self.render_layer = render.RenderLayerPointLightEnvTorch()

        assert self.train is True
        # Optimizer
        self.w_brdf_A = 1
        self.w_brdf_N = 1
        self.w_brdf_R = 0.5
        self.w_brdf_D = 0.5
        self.w_env    = 0.01
        self.w_relit  = 1
        # Optimizer, actually only a group of optimizer
        self.optimizerE = torch.optim.Adam(self.encoder.parameters(), lr=1e-4, betas=(0.5, 0.999))
        self.optimizerBRDF = torch.optim.Adam(self.decoder_brdf.parameters(), lr=2e-4, betas=(0.5, 0.999))
        self.optimizerDRen = torch.optim.Adam(self.decoder_render.parameters(), lr=2e-4, betas=(0.5, 0.999))
        self.optimizerEnv  = torch.optim.Adam(self.env_predictor.parameters(), lr=2e-4, betas=(0.5, 0.999))

        self.error_list_albedo = []
        self.error_list_normal = []
        self.error_list_depth  = []
        self.error_list_rough  = []
        self.error_list_env    = []
        self.error_list_relit  = []
        self.error_list_total  = []

        if self.opts.reuse:
            print('--> loading saved models and loss npys')
            [self.update_lr() for i in range(int(self.opts.start_epoch / 2))]
            self.load_saved_loss(self.opts.start_epoch)
            self.load_saved_checkpoint(self.opts.start_epoch)
        else:
            # loss for saving
            self.error_save_albedo = []
            self.error_save_normal = []
            self.error_save_depth  = []
            self.error_save_rough  = []
            self.error_save_env    = []
            self.error_save_relit  = []
            self.error_save_total  = []
            print('--> start a new model')

    def gen_light_batch_hemi(self, batch_size):
        light = torch.cat([self.gen_uniform_in_hemisphere() for i in range(batch_size)], dim=0)
        light = light.float().cuda()
        return light

    def gen_uniform_in_hemisphere(self):
        """
        Randomly generate an unit 3D vector to represent
        the light direction
        """
        phi = np.random.uniform(0, np.pi * 2)
        costheta = np.random.uniform(0, 1)

        theta = np.arccos(costheta)
        x = np.sin(theta) * np.cos(phi)
        y = np.sin(theta) * np.sin(phi)
        z = np.cos(theta) - 1
        return torch.from_numpy(np.array([[x, y, z]]))

    def gen_light_batch(self, batch_size, range=2, z=0):
        light = np.random.uniform(-1*range, range, (batch_size, 3)); light[:, 2] = z
        light = torch.from_numpy(light).float().cuda()
        return light

    def set_input_var(self, data):
        """setup input var"""        
        self.albedo = data['albedo']
        self.normal = data['normal']
        self.rough  = data['rough']
        self.depth  = data['depth']
        self.mask   = data['seg']
        self.SH     = data['SH']
        self.image_bg = data['image_bg']

        self.light_s = torch.zeros(self.albedo.size(0), 3).float().cuda()
        self.image_s_pe = self.make_image_under_pt_and_env(self.light_s)

        self.aux_pe_images = []
        self.aux_lights = []
        for i in range(self.aux_cnt):
            self.aux_lights.append(self.gen_light_batch_hemi(self.albedo.size(0)))
            self.aux_pe_images.append(self.make_image_under_pt_and_env(self.aux_lights[-1]))

    def make_image_under_env(self):
        image_env = self.render_layer.forward_env(self.albedo, self.normal, self.rough, self.mask, self.SH) + self.image_bg
        image_in = 2 * image_env - 1
        image_in = torch.clamp_(image_in, -1, 1)
        # image under env with bg
        return image_in

    def make_image_under_pt_and_env(self, light):
        image_pt = self.render_layer.forward_batch(self.albedo, self.normal, self.rough, self.depth, self.mask, light) * self.mask
        image_env = self.render_layer.forward_env(self.albedo, self.normal, self.rough, self.mask, self.SH) + self.image_bg
        image_pe = 2 * (image_pt + image_env) - 1
        image_pe = torch.clamp_(image_pe, -1, 1)
        # image under point and env with bg
        return image_pe

    def range(self, tensor):
        print(torch.min(tensor), torch.max(tensor))

    def visual(self, image):
        return image.squeeze().permute(1, 2, 0).cpu().numpy() ** (1/2.2)

    def forward(self):
        """forward: generate all data and GT"""
        # Mode 1
        input = torch.cat([self.image_s_pe, self.image_s_pe * self.mask, self.mask], 1)
        # Mode 2
        # input = torch.cat([self.image_s_pe * self.mask, self.mask], 1)

        # encoder
        feat = self.encoder(input)

        self.env_pred = self.env_predictor(feat[-1])
        brdf_feat, brdf_pred = self.decoder_brdf(feat)
        self.albedo_pred, self.normal_pred, \
        self.rough_pred, self.depth_pred = brdf_pred

        self.aux_preds = []
        for i in range(len(self.aux_lights)):
            light_t = torch.cat([self.aux_lights[i], self.env_pred.view(self.env_pred.size(0), -1)], 1) 
            self.aux_preds += [self.decoder_render(feat, brdf_feat, light_t) * self.mask]

    def compute_loss(self):
        pixel_num = (torch.sum(self.mask).cpu().data).item()

        loss_relit = []
        for i in range(len(self.aux_lights)):
            loss_relit.append(torch.sum((self.aux_preds[i] - self.aux_pe_images[i]) ** 2 * self.mask.expand_as(self.image_s_pe)) / pixel_num / 3.0)
        self.loss_relit = sum(loss_relit)

        self.lossA = torch.sum((self.albedo_pred - self.albedo) ** 2 * self.mask.expand_as(self.albedo)) / pixel_num / 3.0
        self.lossN = torch.sum((self.normal_pred - self.normal) ** 2 * self.mask.expand_as(self.normal)) / pixel_num / 3.0
        self.lossR = torch.sum((self.rough_pred  - self.rough)  ** 2 * self.mask) / pixel_num
        self.lossD = torch.sum((self.depth_pred  - self.depth)  ** 2 * self.mask) / pixel_num

        self.lossE = torch.mean((self.SH - self.env_pred) ** 2)

        self.loss = self.w_brdf_A * self.lossA + \
                    self.w_brdf_N * self.lossN + \
                    self.w_brdf_R * self.lossR + \
                    self.w_brdf_D * self.lossD + \
                    self.w_relit * self.loss_relit + \
                    self.w_env * self.lossE

        self.error_list_albedo.append(self.lossA.item())
        self.error_list_normal.append(self.lossN.item())
        self.error_list_depth.append(self.lossD.item())
        self.error_list_rough.append(self.lossR.item())
        self.error_list_env.append(self.lossE.item())
        self.error_list_relit.append(self.loss_relit.item() / self.aux_cnt)
        self.error_list_total.append(self.loss.item())

    def _backward(self):
        self.compute_loss()
        self.loss.backward()

    def update(self):
        """update"""
        self.optimizerE.zero_grad()
        self.optimizerBRDF.zero_grad()
        self.optimizerDRen.zero_grad()
        self.optimizerEnv.zero_grad()
        # forwarod
        self.forward()
        # update netG
        self._backward()
        self.optimizerE.step()
        self.optimizerBRDF.step()
        self.optimizerDRen.step()
        self.optimizerEnv.step()

    def save_cur_sample(self, epoch):
        path = '%s/%s/state_dict_%s/samples' % (self.opts.outf, self.name, str(epoch))
        if not os.path.exists(path):
            os.makedirs(path)
        vutils.save_image(((((self.image_s_pe+1.0)/2.0))**(1.0/2.2)).data,
                    '{0}/image_src_bg.png'.format(path))        
        vutils.save_image(((self.image_bg)**(1.0/2.2)).data,
                    '{0}/image_bg.png'.format(path))

        for i, (img_pred, img_target) in enumerate(zip(self.aux_preds, self.aux_pe_images)):
            vutils.save_image((((img_pred+1.0)/2.0 * self.mask + self.image_bg)**(1.0/2.2)).data,
                 '{0}/image_pred_{1}.png'.format(path, i))
            vutils.save_image((((img_target+1.0)/2.0 * self.mask + self.image_bg)**(1.0/2.2)).data,
                 '{0}/image_targ_{1}.png'.format(path, i))

        vutils.save_image((0.5*(self.albedo + 1)*self.mask.expand_as(self.albedo)).data,
                    '{0}/albedo_gt.png'.format(path))
        vutils.save_image((0.5*(self.normal + 1)*self.mask.expand_as(self.normal)).data,
                    '{0}/normal_gt.png'.format(path))
        vutils.save_image((0.5*(self.rough  + 1)*self.mask.expand_as(self.rough )).data,
                    '{0}/rough_gt.png'.format(path))
        depth = 1 / torch.clamp(self.depth, 1e-6, 10) * self.mask.expand_as(self.depth)
        depth = (depth - 0.25) / 0.8
        vutils.save_image((depth*self.mask.expand_as(depth)).data,'{0}/depth_gt.png'.format(path))

        vutils.save_image((0.5*(self.albedo_pred + 1)*self.mask.expand_as(self.albedo)).data,
                    '{0}/albedo_pred.png'.format(path))
        vutils.save_image((0.5*(self.normal_pred + 1)*self.mask.expand_as(self.normal)).data,
                    '{0}/normal_pred.png'.format(path))
        vutils.save_image((0.5*(self.rough_pred  + 1)*self.mask.expand_as(self.rough )).data,
                    '{0}/rough_pred.png'.format(path))
        depth = 1 / torch.clamp(self.depth_pred, 1e-6, 10) * self.mask.expand_as(self.depth)
        depth = (depth - 0.25) / 0.8
        vutils.save_image((depth*self.mask.expand_as(depth)).data,'{0}/depth_pred.png'.format(path))

        # vutils.save_image((((self.recst_pred+1.0)/2.0 * self.mask + self.image_bg)**(1.0/2.2)).data,
        #             '{0}/image_recst.png'.format(path))

    def flush_error_npy(self):
        self.error_save_albedo.append(np.mean(self.error_list_albedo))
        self.error_save_normal.append(np.mean(self.error_list_normal))
        self.error_save_depth.append(np.mean(self.error_list_depth))
        self.error_save_rough.append(np.mean(self.error_list_rough))
        self.error_save_relit.append(np.mean(self.error_list_relit))
        self.error_save_total.append(np.mean(self.error_list_total))
        self.error_save_env.append(np.mean(self.error_list_env))

        self.error_list_albedo.clear()
        self.error_list_normal.clear()
        self.error_list_depth.clear()
        self.error_list_rough.clear()
        self.error_list_relit.clear()
        self.error_list_total.clear()
        self.error_list_env.clear()
        
    def save_error_to_file(self, epoch):
        path = '%s/%s/state_dict_%s/errors' % (self.opts.outf, self.name, str(epoch))
        if not os.path.exists(path):
            os.makedirs(path)
        np.save('{0}/albedo_error_{1}.npy'.format(path, epoch), np.array(self.error_save_albedo))
        np.save('{0}/normal_error_{1}.npy'.format(path, epoch), np.array(self.error_save_normal))
        np.save('{0}/rough_error_{1}.npy'.format(path, epoch), np.array(self.error_save_rough))
        np.save('{0}/depth_error_{1}.npy'.format(path, epoch), np.array(self.error_save_depth))
        np.save('{0}/relit_error_{1}.npy'.format(path, epoch), np.array(self.error_save_relit))      
        np.save('{0}/total_error_{1}.npy'.format(path, epoch), np.array(self.error_save_total))   
        np.save('{0}/env_error_{1}.npy'.format(path, epoch), np.array(self.error_save_env))

    def save_cur_checkpoint(self, epoch):
        print('--> saving checkpoints')
        path = '%s/%s/state_dict_%s/models' % (self.opts.outf, self.name, str(epoch))
        if not os.path.exists(path):
            os.makedirs(path)
        torch.save(self.encoder.state_dict(),  '%s/encoder.pth'  % path)
        torch.save(self.decoder_brdf.state_dict(), '%s/decoder_brdf.pth' % path)
        torch.save(self.decoder_render.state_dict(), '%s/decoder_render.pth' % path)
        torch.save(self.env_predictor.state_dict(), '%s/env_predictor.pth' % path)
        print('--> saving done')

    def load_saved_checkpoint(self, start_epoch):
        print('--> loading saved model')
        path = '%s/%s/state_dict_%s/models' % (self.opts.outf, self.name, str(start_epoch-1))
        self.encoder.load_state_dict(torch.load( '%s/encoder.pth'  % path, map_location=lambda storage, loc:storage))
        self.decoder_brdf.load_state_dict(torch.load('%s/decoder_brdf.pth' % path, map_location=lambda storage, loc:storage))
        self.decoder_render.load_state_dict(torch.load('%s/decoder_render.pth' % path, map_location=lambda storage, loc:storage))
        self.env_predictor.load_state_dict(torch.load('%s/env_predictor.pth' % path, map_location=lambda storage, loc:storage))

    def load_saved_loss(self, epoch):
        epoch = epoch - 1
        path = '%s/%s/state_dict_%s/errors' % (self.opts.outf, self.name, str(epoch))
        if not os.path.exists(path):
            raise ValueError('No such files: %s' % path)
        self.error_save_albedo = np.load('{0}/albedo_error_{1}.npy'.format(path, epoch)).tolist()
        self.error_save_normal = np.load('{0}/normal_error_{1}.npy'.format(path, epoch)).tolist()
        self.error_save_rough  = np.load('{0}/rough_error_{1}.npy'.format(path, epoch)).tolist()
        self.error_save_depth  = np.load('{0}/depth_error_{1}.npy'.format(path, epoch)).tolist()
        self.error_save_relit  = np.load('{0}/relit_error_{1}.npy'.format(path, epoch)).tolist()
        self.error_save_total  = np.load('{0}/total_error_{1}.npy'.format(path, epoch)).tolist()
        self.error_save_env    = np.load('{0}/env_error_{1}.npy'.format(path, epoch)).tolist()

    def update_lr(self, rate=2):
        print('--> devide lr by %d' % rate)
        for param_group in self.optimizerE.param_groups:
            param_group['lr'] /= rate
        for param_group in self.optimizerBRDF.param_groups:
            param_group['lr'] /= rate
        for param_group in self.optimizerDRen.param_groups:
            param_group['lr'] /= rate
        for param_group in self.optimizerEnv.param_groups:
            param_group['lr'] /= rate

    def logger_loss(self, epoch, _iter):
        print('%s: [%d/%d][%d/%d], loss: %.4f' % (self.name, epoch, self.opts.nepoch[0], _iter, \
                        self.opts.niter, self.loss.item()))
        print('A: %.3f, N: %.3f, R: %.3f, D: %.3f, relit: %.3f, env: %.3f' % (self.lossA.item(), self.lossN.item(), \
                     self.lossR.item(), self.lossD.item(), self.loss_relit.item(), self.lossE.item()))