from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import sys
import math
import random
import logging
import sklearn
import pickle
import numpy as np
import mxnet as mx
from mxnet import ndarray as nd
import argparse
import mxnet.optimizer as optimizer
from config import config, default, dataset, generate_config
from metric import *
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'common'))
import flops_counter
from helper import Logger
sys.path.append(os.path.join(os.path.dirname(__file__), 'eval'))
import verification
sys.path.append(os.path.join(os.path.dirname(__file__), 'symbol'))
import fresnet
import fmobilefacenet
import fmobilenet
import fmnasnet
import fdensenet
import vargfacenet


logger = logging.getLogger()
logger.setLevel(logging.INFO)


args = None



def parse_args():

  ##-------local config-------##
  os.environ['CUDA_VISIBLE_DEVICES'] = '0,1,2,3'
  default.models_root = '../Experiments/casia-arcface-r50-B110-noarcface_dma-lable-1.0'
  default.network = 'r50'
  default.dataset = 'casia'
  default.loss = 'arcface'

  default.per_batch_size = 110
  dataset.casia.max_steps = 38000
  dataset.casia.lr_steps = '23750, 33250'

  config.fix_gamma = True
  config.detach_diff = False
  config.m_mode = 'default'  # 'default', 'larger_sqrt'
  config.margin = 0.6
  ##-------local config-------##

  parser = argparse.ArgumentParser(description='Train face network')
  # general
  parser.add_argument('--dataset', default=default.dataset, help='dataset config')
  parser.add_argument('--network', default=default.network, help='network config')
  parser.add_argument('--loss', default=default.loss, help='loss config')
  args, rest = parser.parse_known_args()
  generate_config(args.network, args.dataset, args.loss)
  parser.add_argument('--models-root', default=default.models_root, help='root directory to save model.')
  parser.add_argument('--pretrained', default=default.pretrained, help='pretrained model to load')
  parser.add_argument('--pretrained-epoch', type=int, default=default.pretrained_epoch, help='pretrained epoch to load')
  parser.add_argument('--ckpt', type=int, default=default.ckpt, help='checkpoint saving option. 0: discard saving. 1: save when necessary. 2: always save')
  parser.add_argument('--verbose', type=int, default=default.verbose, help='do verification testing and model saving every verbose batches')
  parser.add_argument('--lr', type=float, default=default.lr, help='start learning rate')
  parser.add_argument('--lr-steps', type=str, default=default.lr_steps, help='steps of lr changing')
  parser.add_argument('--wd', type=float, default=default.wd, help='weight decay')
  parser.add_argument('--mom', type=float, default=default.mom, help='momentum')
  parser.add_argument('--frequent', type=int, default=default.frequent, help='')
  parser.add_argument('--per-batch-size', type=int, default=default.per_batch_size, help='batch size in each context')
  parser.add_argument('--kvstore', type=str, default=default.kvstore, help='kvstore setting')
  args = parser.parse_args()

  ##-------local config-------##
  config.loss_m2 = 0.0

  args.angular_loss_classify = False
  args.angular_loss_hidden = False
  args.angular_losstype = 'theta'
  args.angular_loss_weight = 0.03

  args.dma = True
  args.dma_weight = 1.0
  args.dma_type = 'lable'   # 'lable', 'dma'
  ##-------local config-------##

  return args


def get_symbol(args):
  embedding = eval(config.net_name).get_symbol()
  all_label = mx.symbol.Variable('softmax_label')
  gt_label = all_label
  is_softmax = True
  if config.loss_name=='softmax': #softmax
    _weight = mx.symbol.Variable("fc7_weight", shape=(config.num_classes, config.emb_size), 
        lr_mult=config.fc7_lr_mult, wd_mult=config.fc7_wd_mult, init=mx.init.Normal(0.01))
    if config.fc7_no_bias:
      fc7 = mx.sym.FullyConnected(data=embedding, weight = _weight, no_bias = True, num_hidden=config.num_classes, name='fc7')
    else:
      _bias = mx.symbol.Variable('fc7_bias', lr_mult=2.0, wd_mult=0.0)
      fc7 = mx.sym.FullyConnected(data=embedding, weight = _weight, bias = _bias, num_hidden=config.num_classes, name='fc7')
  elif config.loss_name=='margin_softmax':
    _weight = mx.symbol.Variable("fc7_weight", shape=(config.num_classes, config.emb_size), 
        lr_mult=config.fc7_lr_mult, wd_mult=config.fc7_wd_mult, init=mx.init.Normal(0.01))
    s = config.loss_s
    _weight = mx.symbol.L2Normalization(_weight, mode='instance')
    nembedding = mx.symbol.L2Normalization(embedding, mode='instance', name='fc1n')*s
    fc7 = mx.sym.FullyConnected(data=nembedding, weight = _weight, no_bias = True, num_hidden=config.num_classes, name='fc7')
    if config.loss_m1!=1.0 or config.loss_m2!=0.0 or config.loss_m3!=0.0:
      if config.loss_m1==1.0 and config.loss_m2==0.0:
        s_m = s*config.loss_m3
        gt_one_hot = mx.sym.one_hot(gt_label, depth = config.num_classes, on_value = s_m, off_value = 0.0)
        fc7 = fc7-gt_one_hot
      else:
        zy = mx.sym.pick(fc7, gt_label, axis=1)
        cos_t = zy/s
        t = mx.sym.arccos(cos_t)
        if config.loss_m1!=1.0:
          t = t*config.loss_m1
        if config.loss_m2>0.0:
          if config.m_mode == 'larger_sqrt':
            t = t + config.margin * mx.symbol.sqrt(t)
            t = mx.symbol.clip(t, a_min=0., a_max=math.pi)
          else:
            t = t+config.loss_m2
        body = mx.sym.cos(t)
        if config.loss_m3>0.0:
          body = body - config.loss_m3
        new_zy = body*s
        diff = new_zy - zy
        diff = mx.sym.expand_dims(diff, 1)
        gt_one_hot = mx.sym.one_hot(gt_label, depth = config.num_classes, on_value = 1.0, off_value = 0.0)
        body = mx.sym.broadcast_mul(gt_one_hot, diff)
        if config.detach_diff:
            body = mx.symbol.BlockGrad(body)
        fc7 = fc7+body
  elif config.loss_name.find('triplet')>=0:
    is_softmax = False
    nembedding = mx.symbol.L2Normalization(embedding, mode='instance', name='fc1n')
    anchor = mx.symbol.slice_axis(nembedding, axis=0, begin=0, end=args.per_batch_size//3)
    positive = mx.symbol.slice_axis(nembedding, axis=0, begin=args.per_batch_size//3, end=2*args.per_batch_size//3)
    negative = mx.symbol.slice_axis(nembedding, axis=0, begin=2*args.per_batch_size//3, end=args.per_batch_size)
    if config.loss_name=='triplet':
      ap = anchor - positive
      an = anchor - negative
      ap = ap*ap
      an = an*an
      ap = mx.symbol.sum(ap, axis=1, keepdims=1) #(T,1)
      an = mx.symbol.sum(an, axis=1, keepdims=1) #(T,1)
      triplet_loss = mx.symbol.Activation(data = (ap-an+config.triplet_alpha), act_type='relu')
      triplet_loss = mx.symbol.mean(triplet_loss)
    else:
      ap = anchor*positive
      an = anchor*negative
      ap = mx.symbol.sum(ap, axis=1, keepdims=1) #(T,1)
      an = mx.symbol.sum(an, axis=1, keepdims=1) #(T,1)
      ap = mx.sym.arccos(ap)
      an = mx.sym.arccos(an)
      triplet_loss = mx.symbol.Activation(data = (ap-an+config.triplet_alpha), act_type='relu')
      triplet_loss = mx.symbol.mean(triplet_loss)
    triplet_loss = mx.symbol.MakeLoss(triplet_loss)
  out_list = [mx.symbol.BlockGrad(embedding)]
  if is_softmax:
    softmax = mx.symbol.SoftmaxOutput(data=fc7, label = gt_label, name='softmax', normalization='valid')
    out_list.append(softmax)
    if config.ce_loss:
      #ce_loss = mx.symbol.softmax_cross_entropy(data=fc7, label = gt_label, name='ce_loss')/args.per_batch_size
      body = mx.symbol.SoftmaxActivation(data=fc7)
      body = mx.symbol.log(body)
      _label = mx.sym.one_hot(gt_label, depth = config.num_classes, on_value = -1.0, off_value = 0.0)
      body = body*_label
      ce_loss = mx.symbol.sum(body)/args.per_batch_size
      out_list.append(mx.symbol.BlockGrad(ce_loss))
  else:
    out_list.append(mx.sym.BlockGrad(gt_label))
    out_list.append(triplet_loss)

  # --- add angular loss --- #
  if args.angular_loss_classify or args.angular_loss_hidden:
      loss = 0
      if args.angular_loss_classify:
          # Remove diagnonal from loss
          product = mx.symbol.linalg.syrk(_weight, alpha=1., transpose=False) - 2. * mx.symbol.eye(config.num_classes)
          # Minimize maximum cosine similarity.
          if args.angular_losstype == 'cosine':
            loss = mx.symbol.mean(mx.symbol.max(product, axis=1))
          elif args.angular_losstype == 'theta':
            theta = mx.symbol.arccos(mx.symbol.max(product, axis=1))
            loss = -mx.symbol.mean(theta)

      if args.angular_loss_hidden:
          internals = embedding.get_internals()
          internals_list = embedding.get_internals().list_outputs()
          for i in range(0, len(internals_list)):
              if 'weight' in internals_list[i]:
                  # get angular loss
                  loss = loss + get_angular_loss(internals[i])

      angular_loss = mx.symbol.MakeLoss(loss, grad_scale=args.angular_loss_weight)
      out_list.append(angular_loss)
  # --- add angular loss --- #

  # --- add DMA loss ---#
  if args.dma:
      # weight_ = mx.symbol.BlockGrad(_weight)
      dma_cosine = mx.sym.FullyConnected(data=nembedding, weight=_weight, no_bias=True, num_hidden=config.num_classes) / s
      dma_loss_value = closer_loss(dma_cosine, gt_label, args.dma_type)

      dma_loss = mx.symbol.MakeLoss(dma_loss_value, grad_scale=args.dma_weight)
      out_list.append(dma_loss)
  # --- add DMA loss ---#

  out = mx.symbol.Group(out_list)

  return out


def closer_loss(cosine, gt_label, type):

    if type == 'lable':
        theta = mx.symbol.arccos(mx.sym.pick(cosine, gt_label, axis=1))
    elif type == 'dma':
        theta = mx.symbol.arccos(mx.symbol.max(cosine, axis=1))

    loss = mx.symbol.mean(0.5 * mx.symbol.square(theta))

    return loss


def get_angular_loss(weight):
    '''
    :param weight: parameter of model, out_features *　in_features
    :return: angular loss
    '''

    if 'conv' in weight.name:
        # for convolution layers, flatten
        num_filter = int(weight.attr('num_filter'))
        weight = weight.reshape((num_filter, -1))
    else:
        num_filter = int(weight.attr('num_hidden'))

    # Dot product of normalized prototypes is cosine similarity.
    weight_ = mx.symbol.L2Normalization(weight, mode='instance')
    product = mx.symbol.linalg.syrk(weight_, alpha=1., transpose=False) - 2. * mx.symbol.eye(num_filter)
    theta = mx.symbol.arccos(mx.symbol.max(product, axis=1))
    loss = -mx.symbol.mean(theta)

    return loss


def train_net(args):
    ctx = []
    cvd = os.environ['CUDA_VISIBLE_DEVICES'].strip()
    if len(cvd)>0:
      for i in range(len(cvd.split(','))):
        ctx.append(mx.gpu(i))
    if len(ctx)==0:
      ctx = [mx.cpu()]
      print('use cpu')
    else:
      print('gpu num:', len(ctx))
    prefix = os.path.join(args.models_root, '%s-%s-%s'%(args.network, args.loss, args.dataset), 'model')
    prefix_dir = os.path.dirname(prefix)
    print('prefix', prefix)
    if not os.path.exists(prefix_dir):
      os.makedirs(prefix_dir)
    sys.stdout = Logger(prefix_dir + '/log.txt')
    args.ctx_num = len(ctx)
    args.batch_size = args.per_batch_size*args.ctx_num
    args.rescale_threshold = 0
    args.image_channel = config.image_shape[2]
    config.batch_size = args.batch_size
    config.per_batch_size = args.per_batch_size

    data_dir = config.dataset_path
    path_imgrec = None
    path_imglist = None
    image_size = config.image_shape[0:2]
    assert len(image_size)==2
    assert image_size[0]==image_size[1]
    print('image_size', image_size)
    print('num_classes', config.num_classes)
    path_imgrec = os.path.join(data_dir, "train.rec")

    print('Called with argument:', args, config)
    data_shape = (args.image_channel,image_size[0],image_size[1])
    mean = None

    begin_epoch = 0
    if len(args.pretrained)==0:
      arg_params = None
      aux_params = None
      sym = get_symbol(args)
      if config.net_name=='spherenet':
        data_shape_dict = {'data' : (args.per_batch_size,)+data_shape}
        spherenet.init_weights(sym, data_shape_dict, args.num_layers)
    else:
      print('loading', args.pretrained, args.pretrained_epoch)
      _, arg_params, aux_params = mx.model.load_checkpoint(args.pretrained, args.pretrained_epoch)
      sym = get_symbol(args)

    if config.count_flops:
      all_layers = sym.get_internals()
      _sym = all_layers['fc1_output']
      FLOPs = flops_counter.count_flops(_sym, data=(1,3,image_size[0],image_size[1]))
      _str = flops_counter.flops_str(FLOPs)
      print('Network FLOPs: %s'%_str)

    #label_name = 'softmax_label'
    #label_shape = (args.batch_size,)
    model = mx.mod.Module(
        context       = ctx,
        symbol        = sym,
    )
    val_dataiter = None

    if config.loss_name.find('triplet')>=0:
      from triplet_image_iter import FaceImageIter
      triplet_params = [config.triplet_bag_size, config.triplet_alpha, config.triplet_max_ap]
      train_dataiter = FaceImageIter(
          batch_size           = args.batch_size,
          data_shape           = data_shape,
          path_imgrec          = path_imgrec,
          shuffle              = True,
          rand_mirror          = config.data_rand_mirror,
          mean                 = mean,
          cutoff               = config.data_cutoff,
          ctx_num              = args.ctx_num,
          images_per_identity  = config.images_per_identity,
          triplet_params       = triplet_params,
          mx_model             = model,
      )
      if args.angular_loss_classify or args.angular_loss_hidden:
          indice=-2
      else:
          indice=-1
      _metric = LossValueMetric(indice)
      eval_metrics = [mx.metric.create(_metric)]
    else:
      from image_iter import FaceImageIter
      train_dataiter = FaceImageIter(
          batch_size           = args.batch_size,
          data_shape           = data_shape,
          path_imgrec          = path_imgrec,
          shuffle              = True,
          rand_mirror          = config.data_rand_mirror,
          mean                 = mean,
          cutoff               = config.data_cutoff,
          color_jittering      = config.data_color,
          images_filter        = config.data_images_filter,
      )
      metric1 = AccMetric()
      eval_metrics = [mx.metric.create(metric1)]
      if config.ce_loss:
        if args.angular_loss_classify or args.angular_loss_hidden:
              indice = -2
        else:
              indice = -1
        metric2 = LossValueMetric(indice)
        eval_metrics.append( mx.metric.create(metric2) )

    if args.angular_loss_classify or args.angular_loss_hidden:
      metric3 = LossValueMetric()
      eval_metrics.append(mx.metric.create(metric3))

    if config.net_name=='fresnet' or config.net_name=='fmobilefacenet':
      initializer = mx.init.Xavier(rnd_type='gaussian', factor_type="out", magnitude=2) #resnet style
    else:
      initializer = mx.init.Xavier(rnd_type='uniform', factor_type="in", magnitude=2)
    #initializer = mx.init.Xavier(rnd_type='gaussian', factor_type="out", magnitude=2) #resnet style
    _rescale = 1.0/args.ctx_num
    opt = optimizer.SGD(learning_rate=args.lr, momentum=args.mom, wd=args.wd, rescale_grad=_rescale)
    _cb = mx.callback.Speedometer(args.batch_size, args.frequent)

    ver_list = []
    ver_name_list = []
    for name in config.val_targets:
      path = os.path.join(data_dir,name+".bin")
      if os.path.exists(path):
        data_set = verification.load_bin(path, image_size)
        ver_list.append(data_set)
        ver_name_list.append(name)
        print('ver', name)



    def ver_test(nbatch):
      results = []
      for i in range(len(ver_list)):
        acc1, std1, acc2, std2, xnorm, embeddings_list = verification.test(ver_list[i], model, args.batch_size, 10, None, None)
        print('[%s][%d]XNorm: %f' % (ver_name_list[i], nbatch, xnorm))
        #print('[%s][%d]Accuracy: %1.5f+-%1.5f' % (ver_name_list[i], nbatch, acc1, std1))
        print('[%s][%d]Accuracy-Flip: %1.5f+-%1.5f' % (ver_name_list[i], nbatch, acc2, std2))
        results.append(acc2)
      return results



    highest_acc = [0.0, 0.0]  #lfw and target
    #for i in range(len(ver_list)):
    #  highest_acc.append(0.0)
    global_step = [0]
    save_step = [0]
    lr_steps = [int(x) for x in args.lr_steps.split(',')]
    print('lr_steps', lr_steps)
    def _batch_callback(param):
      #global global_step
      global_step[0]+=1
      mbatch = global_step[0]
      for step in lr_steps:
        if mbatch==step:
          opt.lr *= 0.1
          print('lr change to', opt.lr)
          break

      _cb(param)
      if mbatch%1000==0:
        print('lr-batch-epoch:',opt.lr,param.nbatch,param.epoch)

      if mbatch>=0 and mbatch%args.verbose==0:
        acc_list = ver_test(mbatch)
        save_step[0]+=1
        msave = save_step[0]
        do_save = False
        is_highest = False
        if len(acc_list)>0:
          #lfw_score = acc_list[0]
          #if lfw_score>highest_acc[0]:
          #  highest_acc[0] = lfw_score
          #  if lfw_score>=0.998:
          #    do_save = True
          score = sum(acc_list)
          if acc_list[-1]>=highest_acc[-1]:
            if acc_list[-1]>highest_acc[-1]:
              is_highest = True
            else:
              if score>=highest_acc[0]:
                is_highest = True
                highest_acc[0] = score
            highest_acc[-1] = acc_list[-1]
            #if lfw_score>=0.99:
            #  do_save = True
        if is_highest:
          do_save = True
        if args.ckpt==0:
          do_save = False
        elif args.ckpt==2:
          do_save = True
        elif args.ckpt==3:
          msave = 1

        if do_save:
          print('saving', msave)
          arg, aux = model.get_params()
          if config.ckpt_embedding:
            all_layers = model.symbol.get_internals()
            _sym = all_layers['fc1_output']
            _arg = {}
            for k in arg:
              if not k.startswith('fc7'):
                _arg[k] = arg[k]
            mx.model.save_checkpoint(prefix, msave, _sym, _arg, aux)
          else:
            mx.model.save_checkpoint(prefix, msave, model.symbol, arg, aux)
        print('[%d]Accuracy-Highest: %1.5f'%(mbatch, highest_acc[-1]))
      sys.stdout.flush()
      if config.max_steps>0 and mbatch>config.max_steps:
        sys.exit(0)

    epoch_cb = None
    train_dataiter = mx.io.PrefetchingIter(train_dataiter)

    model.fit(train_dataiter,
        begin_epoch        = begin_epoch,
        num_epoch          = 999999,
        eval_data          = val_dataiter,
        eval_metric        = eval_metrics,
        kvstore            = args.kvstore,
        optimizer          = opt,
        #optimizer_params   = optimizer_params,
        initializer        = initializer,
        arg_params         = arg_params,
        aux_params         = aux_params,
        allow_missing      = True,
        batch_end_callback = _batch_callback,
        epoch_end_callback = epoch_cb )

def main():
    global args
    args = parse_args()
    train_net(args)

if __name__ == '__main__':
    main()

