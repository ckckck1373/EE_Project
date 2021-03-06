###########################################
#      Quantize the CNN net               #
#                                         #
#        Huang Ching, Wang                #
#          nthu EE  VCS LAB               #
#      ref: ristretto caffe               #
###########################################
import torch
import torchvision
import torchvision.transforms as transforms
import matplotlib.pyplot as plt
import numpy as np
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import csv
import time
import argparse
import copy
import os
# from lenet import *
from model import *
#
#from data_setting import *
from dataset_train import *
import skimage
import skimage.io as sio
import scipy.misc
from calc_psnr import calc_psnr
from train import train_data_loader


parser = argparse.ArgumentParser(description='NTHU EE - project - onepiece SRNet')
parser.add_argument('--model', type=str, required=True, help='model file to use')
parser.add_argument('--bw_param', type=int, required=True, help='bw_param?')
parser.add_argument('--bw_activation', type=int, required=True, help='bw_activation?')
parser.add_argument('--quan_param_path', type=str, required=True, help='quan_param_path?')

args = parser.parse_args()


# def test_accuracy(model, testloader):
#     correct = 0
#     total = 0
    # for data in testloader:
    #     images, labels = data
    #     outputs = model(Variable(images.cuda()))
    #     _, predicted = torch.max(outputs.data, 1) # return every row's maximum element
    #     total += labels.size(0)
    #     correct += (predicted == labels.cuda()).sum() # ?
        
    #print('Accuracy of the network on the 10000 test images: %f %%' % (100 * correct.cpu().numpy() / total))

def test_psnr(model):
    base = "F24B4" # FXXBX
    if args.cuda:
        model = model.cuda()
    
    #===== Load input image =====
    for iter_onepiece in range(1,17):
        if iter_onepiece < 10:
            output_file_name = 'result/' + str(args.screen_num) + '/F24B4_onepiece_test_000' + str(iter_onepiece) + '.png'
            input_file_name = 'image_test/LR_onepiece_test_000' + str(iter_onepiece) + '.png'
            ref_file_name = 'ref/HR_onepiece_test_000' + str(iter_onepiece) + '.png'
        ## the landscape and more people part (12~16)
        elif (iter_onepiece >= 10)&(iter_onepiece < 17):
            output_file_name = 'result/' + str(args.screen_num) + '/' + base + '_onepiece_test_00' + str(iter_onepiece) + '.png'
            input_file_name = 'image_test/LR_onepiece_test_00' + str(iter_onepiece) + '.png'
            ref_file_name = 'ref/HR_onepiece_test_00' + str(iter_onepiece) + '.png'
        else: 
            print('There are some errors if you see this text.')

        imgIn = sio.imread(input_file_name)
        imgIn = imgIn.transpose((2,0,1)).astype(float)
        imgIn = imgIn.reshape(1,imgIn.shape[0],imgIn.shape[1],imgIn.shape[2])
        imgIn = torch.Tensor(imgIn)

        #===== Test procedures =====
        varIn = Variable(imgIn)
        if args.cuda:
            varIn = varIn.cuda()

        prediction = model(varIn)
        prediction = prediction.data.cpu().numpy().squeeze().transpose((1,2,0))

        scipy.misc.toimage(prediction, cmin=0.0, cmax=255.0).save(args.output_filename)


        #===== Ground-truth comparison =====
        if args.compare_image is not None:
            imgTar = sio.imread(args.compare_image)
            prediction = sio.imread(args.output_filename)  # read the trancated image
            psnr = calc_psnr(prediction, imgTar, max_val=255.0)

            print('===> PSNR: %.3f dB'%(psnr))
            psnr_average = psnr_average + psnr 

    print(psnr_average/16)



# According to the max value to do the quantization 
def dynamic_quan(num_group, bit_width):
    '''Quantize a group of numbers into [min, max] in bit_num bits of expression'''
    num_min = torch.abs(num_group.min())
    num_max = torch.abs(num_group.max())

    if(num_max >= num_min):
        max_val_abs = num_max
    else:
        max_val_abs = num_min

    if max_val_abs <= 0.0001:
        max_val_abs = torch.tensor(0.0001).cuda() ## why

    int_bit = torch.ceil(torch.log2(max_val_abs) + 1)
    fractional_length = bit_width - int_bit

    # use empirical ways to find best SQNR
    new_fra_length = find_best_sqnr(num_group, bit_width, fractional_length)

    interval = 2 ** (-1 * new_fra_length)
    half_interval = interval / 2
    max_val = (2 ** (bit_width - 1) - 1) * interval
    min_val = - (2 ** (bit_width - new_fra_length - 1))

    quan_group = torch.floor((num_group + half_interval) / interval)
    quan_group = quan_group * interval

    quan_group[quan_group >= max_val] = max_val
    quan_group[quan_group <= min_val] = min_val
    num_group.copy_(quan_group)
    return new_fra_length.item()

def find_best_sqnr(num_group, bit_width, fractional_length):
    best_sqnr = 0
    best_i = 0
    P_num = torch.sum(num_group ** 2)

    for i in range(5):
        interval = 2 ** (-1 * (fractional_length + i))
        half_interval = interval / 2
        max_val = (2 ** (bit_width - 1) - 1) * interval
        min_val = - (2 ** (bit_width - (fractional_length + i) - 1))

        quan_group = torch.floor((num_group + half_interval) / interval)
        quan_group = quan_group * interval
        quan_group[quan_group >= max_val] = max_val
        quan_group[quan_group <= min_val] = min_val
        diff = num_group - quan_group
        P_err = torch.sum(diff ** 2)
        sqnr = P_num / P_err

        if sqnr > best_sqnr:
            best_sqnr = sqnr
            best_i = i
    return fractional_length + best_i

# turn to binary bits
def bindigits(n, bits):
    s = bin(n & int("1"*bits, 2))[2:]
    return ("{0:0>%s}" % (bits)).format(s)

# model = Net()
model = torch.load(args.model)


if torch.cuda.is_available():
    model.cuda()
    model = torch.nn.DataParallel(model)

# load the pretrained model
print('===> Loading model')
# savepoint = torch.load(args.model)
# ori_state_dict = savepoint['state_dict']
# model.load_state_dict(ori_state_dict)

# savepoint = torch.load(args.model)
# ori_state_dict = savepoint['state_dict']
# model.load_state_dict(savepoint)

# original accuracy(before quantize)
print("Before quantization:")

# test_accuracy(model, testloader)
test_psnr(model)

print('=====================================')
print('===> Quantizing the parameter to %d bits, and the following is the information of fractional length per layer:' %(args.bw_param))

# copy from the original parameter
new_state_dict = copy.deepcopy(ori_state_dict)

# Quantize the weights and biases
param_fl_dict = {}

for target_name, layer_param in new_state_dict.items():
    target_new_name = target_name.split(".")
    key_name = target_new_name[1] + "_" + target_new_name[2]
    fra_len = dynamic_quan(layer_param, args.bw_param)
    param_fl_dict[key_name] = fra_len
    # save parameters as .npy
    #save_name = "./sim_software/param/" + key_name + ".npy"
    #np.save(save_name, layer_param)
print(param_fl_dict)

# test the parameter, after quantizing the weights and biases
model.load_state_dict(new_state_dict)
print("After quantizing the weights and biases:")
# test_accuracy(model, testloader)
test_psnr(model)

print('=====================================')
print('===> Quantizing the parameter to %d bits and the activation to %d bits.' %(args.bw_param, args.bw_activation))

max_dict = {}
fl_dict = {}

# First, collect the activation from 16 training image, then get the maximum value and record it 
for index, data in enumerate(train_data_loader):
    images = data
    images = Variable(images.cuda()), Variable(labels.cuda())
    # Because I set the batch size to 16, I only need one set of training images
    if(index == 1):
        data_box = model.module.collect_data(images)
        for layer_name, param in data_box.items():
            print(layer_name + ': ' + str(np.max(param.data.cpu().numpy())))
            max_dict[layer_name] = np.max(param.data.cpu().numpy())



# according to max value to get fractional length
print("The following is the information for fractional part:")
for layer_name in max_dict:
    fl_dict[layer_name] = int(args.bw_activation) - np.ceil(np.log2(max_dict[layer_name]))
    print(layer_name + " need %d bits" % fl_dict[layer_name])




# Start to quantize all the network to fixed point
correct = 0
total = 0
for data in testloader:
    images, labels = data
    images, labels = Variable(images.cuda()), Variable(labels.cuda())
    outputs = model.module.quan_forward(images, fl_dict)
    _, predicted = torch.max(outputs.data, 1)
    total += labels.size(0)
    correct += (predicted == labels.data).sum()
    accuracy = (100 * correct.cpu().numpy() / total)
print('Accuracy of the quantized network on the 10000 test images: %.3f %%' % accuracy)
 


# output parameters to fixed point
# according to the fractional length, shift all parameters to integer 
if not os.path.isdir(args.quan_param_path):
    os.mkdir(args.quan_param_path)
for target_name, layer_param in new_state_dict.items():
    target_new_name = target_name.split(".")
    key_name = target_new_name[1] + "_" + target_new_name[2]
    int_param = (layer_param.cpu().numpy() * (2**param_fl_dict[key_name])).astype('int32')

    f = open(args.quan_param_path + '/' + key_name + ".dat", 'w')
    # seperate the weight and bias, and reshape the weight data to 2 dimension.
    # CONV has (chin, chout, kernel_height, kernel_width)
    if(len(int_param.shape) > 2):    
        size_1 = int(int_param.shape[0]) * int(int_param.shape[1])
        size_2 = int(int_param.shape[2]) * int(int_param.shape[3])
        re_param = np.reshape(int_param, (size_1, size_2))
        re_param = re_param.astype('int32')
        for i in range(re_param.shape[0]):
            for j in range(re_param.shape[1]):
                if(j != re_param.shape[1]-1): 
                    f.write(bindigits(re_param[i, j], args.bw_param)+'_')
                else: 
                    f.write(bindigits(re_param[i, j], args.bw_param))
            f.write('\n')
    # FC has (neuron_in, neuron_out)
    elif(len(int_param.shape) == 2):
        for i in range(int_param.shape[0]):
            for j in range(int_param.shape[1]):
                if(j != int_param.shape[1]-1): 
                    f.write(bindigits(int_param[i, j], args.bw_param)+'_')
                else: 
                    f.write(bindigits(int_param[i, j], args.bw_param))
            f.write('\n')
    # bias only has one dimension
    else:
        print(target_name)
        print(int_param.shape)
        for i in range(int_param.shape[0]): 
            f.write(bindigits(int_param[i], args.bw_param)+'\n')
    f.close()