import argparse
import logging
import sys
import time

import numpy as np
from libs.data import VOCdataset
from libs.net import Darknet_test
from torchvision import transforms
from torch.optim.lr_scheduler import StepLR
from torch.autograd import Variable

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import pdb
import os


parser = argparse.ArgumentParser(description='PyTorch YOLOv2')
parser.add_argument('--anchor_scales', type=str,
                    default=('1.3221,1.73145,'
                             '3.19275,4.00944,'
                             '5.05587,8.09892,'
                             '9.47112,4.84053,'
                             '11.2364,10.0071'),
                    help='anchor scales')
parser.add_argument('--resume', type=str, default=None,
                    help='path to latest checkpoint')
parser.add_argument('--start_epoch', default=0, type=int,
                    help='manual epoch number (useful on restarts)')
parser.add_argument('--epochs', type=int, default=25,
                    help='number of total epochs to run')
parser.add_argument('--lr', type=float, default=100000,
                    help='base learning rate')
parser.add_argument('--num_classes', type=int, default=20,
                    help='number of classes')
parser.add_argument('--num_anchors', type=int, default=5,
                    help='number of anchors per cell')
parser.add_argument('--weight_decay', type=float, default=0.0005,
                    help='weight of l2 regularize')
parser.add_argument('--bbox_loss_weight', type=float, default=5.0,
                    help='weight of bbox loss')
parser.add_argument('--batch_size', type=int, default=1,
                    help='batch_size must be 1')


logger = logging.getLogger()
fmt = logging.Formatter('%(asctime)s %(levelname)-8s: %(message)s')
file_handler = logging.FileHandler('train.log')
file_handler.setFormatter(fmt)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(fmt)
logger.addHandler(file_handler)
logger.addHandler(console_handler)
logger.setLevel(logging.INFO)


def transform_center(xy):
    b, h, w, num_anchors, _ = xy.size()
    x = xy[..., 0]
    y = xy[..., 1]
    offset_x = torch.arange(w).view(1, 1, w, 1, 1)
    offset_y = torch.arange(h).view(1, h, 1, 1, 1)
    x = (x + offset_x)/w
    y = (y + offset_y)/h
    return x, y


def transform_size(wh, anchor_scales):
    b, h, w, num_anchors, _ = wh.size()
    return torch.exp(wh[..., 0])*anchor_scales[:, 0]/w, \
           torch.exp(wh[..., 1])*anchor_scales[:, 1]/h

def iou(anchors, gt, h, w):
    anchors_xmax = anchors[..., 0]+0.5*anchors[..., 2]
    anchors_xmin = anchors[..., 0]-0.5*anchors[..., 2]
    anchors_ymax = anchors[..., 1]+0.5*anchors[..., 3]
    anchors_ymin = anchors[..., 1]-0.5*anchors[..., 3]

    # clip value to (0, w/h)
    np.clip(anchors_xmax, 0, w, out=anchors_xmax)
    np.clip(anchors_xmin, 0, w, out=anchors_xmin)
    np.clip(anchors_ymax, 0, h, out=anchors_ymax)
    np.clip(anchors_ymin, 0, h, out=anchors_ymin)

    tb = np.minimum(anchors_xmax, gt[0]+0.5*gt[2])-np.maximum(anchors_xmin, gt[0]-0.5*gt[2])
    lr = np.minimum(anchors_ymax, gt[1]+0.5*gt[3])-np.maximum(anchors_ymin, gt[1]-0.5*gt[3])
    intersection = tb * lr
    intersection[np.where((tb < 0) & (lr < 0))] = 0
    return intersection / (anchors[..., 2]*anchors[..., 3] + gt[2]*gt[3] - intersection)


# def iou(box1, box2):
#     tb = np.minimum(box1[..., 0]+0.5*box1[..., 2],
#                     box2[0]+0.5*box2[2])-np.maximum(box1[..., 0]-0.5*box1[..., 2],
#                                                     box2[0]-0.5*box2[2])

#     lr = np.minimum(box1[..., 1]+0.5*box1[..., 3],
#                     box2[1]+0.5*box2[3])-np.maximum(box1[..., 1]-0.5*box1[..., 3],
#                                                     box2[1]-0.5*box2[3])

#     intersection = tb * lr
#     intersection[np.where((tb < 0) & (lr < 0))] = 0
#     return intersection / (box1[..., 2]*box1[..., 3] + box2[2]*box2[3] - intersection)


def build_target(out_shape, gt, anchor_scales, threshold=0.5):
    num_gts = gt.size()[1]
    b, h, w, n, _ = out_shape

    # dtype must be np.float32(torch.floattensor) for l1_loss
    target_bbox = np.zeros((b, h, w, n, 4), dtype=np.float32)
    object_mask = np.zeros((b, h, w, n, 1), dtype=np.float32)
    iou_mask = np.zeros((b, h, w, n), dtype=np.float32)
    anchors = np.zeros((h, w, n, 4), dtype=np.float32)

    # dtype must be np.int64(torch.Long) for cross entropy
    target_class = np.zeros((b*h*w*n,), dtype=np.int64)

    anchors[..., 0] += np.arange(0.5, w, 1).reshape(1, w, 1)
    anchors[..., 1] += np.arange(0.5, h, 1).reshape(h, 1, 1)
    anchors[..., 2:] += anchor_scales

    for i in range(num_gts):
        # pdb.set_trace()
        gt_x = (gt[0, i, 0]+gt[0, i, 2])/2
        gt_y = (gt[0, i, 1]+gt[0, i, 3])/2
        gt_w = gt[0, i, 2]-gt[0, i, 0]
        gt_h = gt[0, i, 3]-gt[0, i, 1]
        gt_x, gt_y, gt_w, gt_h = gt_x*w, gt_y*h, gt_w*w, gt_h*h

        ious = iou(anchors, np.array([gt_x, gt_y, gt_w, gt_h], dtype=np.float32), h, w)
        flatten_idxs = np.argmax(ious)
        multidim_idxs = np.unravel_index(flatten_idxs, (h, w, n))

        # if iou of best match < threshold we ignore it
        if ious[multidim_idxs[0], multidim_idxs[1], multidim_idxs[2]] > threshold:
            object_mask[0, multidim_idxs[0], multidim_idxs[1], multidim_idxs[2]] = 1
        
            # 0.1 is the weight of iou loss when ious less than threshold(here is 0.5)

            # an anchor with any ground_truth's iou > threshold and 
            # ignore this anchor for iou loss compute? (yes)
            iou_mask[0][np.where(ious < threshold)] = 0.1
            
            iou_mask[0][np.where((ious > threshold) & (ious == -1.0))] = 0            
            # 5.0 is the weight of iou loss when anchors is the best match
            iou_mask[0, multidim_idxs[0], multidim_idxs[1], multidim_idxs[2]] = 5.0

            tx, ty = gt_x-np.floor(gt_x), gt_y-np.floor(gt_y)
            tw = np.log(gt_w/anchor_scales[multidim_idxs[2]][0])
            th = np.log(gt_h/anchor_scales[multidim_idxs[2]][1])
            target_bbox[0, multidim_idxs[0], multidim_idxs[1], multidim_idxs[2]] = tx, ty, tw, th
            target_class[flatten_idxs] = gt[0, i, 4]

    return object_mask, target_bbox, target_class, iou_mask


def save_fn(state, filename='./yolov2.pth.tar'):
    torch.save(state, filename)


def train(train_loader, eval_loader, model, anchor_scales, epochs, opt):
    lr_scheduler = StepLR(opt, step_size=30, gamma=0.1)

    for epoch in range(args.start_epoch, epochs):
        lr_scheduler.step()
        model.train()

        for idx, (imgs, labels) in enumerate(train_loader):
            # imgs = imgs.cuda()
            opt.zero_grad()
            with torch.enable_grad():
                bbox_pred, iou_pred, prob_pred = model(imgs)
            
            object_mask, target_bbox, target_class, iou_mask = \
                build_target(bbox_pred.size(), labels, anchor_scales)
            # pdb.set_trace()
            # object_mask = Variable(torch.from_numpy(object_mask))
            # target_bbox = Variable(torch.from_numpy(target_bbox))
            # target_class = Variable(torch.from_numpy(target_class))
            # iou_mask = Variable(torch.from_numpy(iou_mask))
            # pdb.set_trace()
            with torch.enable_grad():
                bbox_loss = (object_mask*F.l1_loss(bbox_pred,
                                                   target_bbox,
                                                   reduce=False)).sum()
                prob_loss = (F.cross_entropy(prob_pred.view(-1, args.num_classes),
                                             target_class,
                                             reduce=False)*object_mask.view(-1)).sum()
                iou_loss = (iou_mask*F.l1_loss(iou_pred,
                                               object_mask,
                                               reduce=False)).sum()
                loss = bbox_loss+prob_loss+iou_loss
            loss.backward()
            opt.step()

            if idx % 10 == 0:
                logger.info('epoch:{} step:{} bbox_loss:{:.6f} prob_loss:{:.6f} iou_loss:{:.6f}'.format(
                    epoch, idx, bbox_loss.item(), prob_loss.item(), iou_loss.item()))

        save_fn({'epoch': epoch+1,
                 'state_dict': model.state_dict(),
                 'optimizer': opt.state_dict()})


def main():
    global args
    args = parser.parse_args()
    assert args.batch_size == 1
    anchor_scales = map(float, args.anchor_scales.split(','))
    anchor_scales = np.array(list(anchor_scales)).reshape(-1, 2)

    data_transform = {
        'train': transforms.Compose(
            [
                transforms.Resize((416, 416)),
                transforms.ToTensor(), 
                transforms.Normalize([0.5, 0.5, 0.5], [0.5, 0.5, 0.5]),
            ]),
        'val': transforms.Compose(
            [
                transforms.Resize((416, 416)),
                transforms.ToTensor(),
                transforms.Normalize([0.5, 0.5, 0.5], [0.5, 0.5, 0.5]),
            ])
                    }
    train_dataset = VOCdataset(usage='train', transform=data_transform['train'])
    train_loader = torch.utils.data.DataLoader(train_dataset,
                                               batch_size=args.batch_size,
                                               shuffle=True,
						                       num_workers=4,
                                               pin_memory=True,
                                               drop_last=True)
    eval_dataset = VOCdataset(usage='eval', transform=data_transform['val'])
    eval_loader = torch.utils.data.DataLoader(eval_dataset,
                                              batch_size=args.batch_size,
                                              shuffle=False,
						                      num_workers=4,
                                              pin_memory=True,
                                              drop_last=True)

    darknet = Darknet_test(3, args.num_anchors, args.num_classes)
    optimizer = optim.SGD(darknet.parameters(),
                          lr=args.lr,
                          weight_decay=args.weight_decay)

    if args.resume:
        if os.path.isfile(args.resume):
            print("load checkpoint from '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint['epoch']
            net.load_state_dict(checkpoint['state_dict'])
            if not args.test:
                optimizer.load_state_dict(checkpoint['optimizer'])
            print("loaded checkpoint '{}' (epoch {})".format(
                args.resume, checkpoint['epoch']))
        else:
            print("no checkpoint found at '{}'".format(args.resume))
    # print('train')
    train(train_loader,
          eval_loader,
          darknet,
          anchor_scales,
          epochs=args.epochs,
          opt=optimizer)


if __name__ == '__main__':
    main()