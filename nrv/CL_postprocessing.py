"""
NRV-Cellular Level postprocessing
Authors: Florian Kolbl / Roland Giraud / Louis Regnacq
(c) ETIS - University Cergy-Pontoise - CNRS
"""
import faulthandler
from collections.abc import Iterable
import matplotlib.pyplot as plt
import numpy as np
from pylab import argmin,argmax
from scipy import signal
from numba import jit
from .file_handler import json_dump, json_load, is_iterable
from .log_interface import rise_error, rise_warning, pass_info

# enable faulthandler to ease 'segmentation faults' debug
faulthandler.enable()

#############################
## miscellaneous functions ##
#############################
def distance_point2line(x_p,y_p,a,b):
    '''
    Computes the distance between a point (x_p,y_p) and a line defined as y=a*x+b

    Parameters
    ----------
    x_p : float
        point x coordinate,
    y_p : float
        point y coordinate,
    a   : float
        line direction coeefficient
    b   : float
        line y for x = 0

    Returns
    --------
    d : float
        distance between the point and the orthogonal projection of (x_p,y_p) on it
    '''
    d = np.abs(a*x_p - y_p + b)/(np.sqrt(a**2 + 1))
    return d

#####################################
## SAVE AND LOAD RESULTS FUNCTIONS ##
#####################################
def save_axon_results_as_json(results,filename):
    """
    save dictionary as a json file

    Parameters
    ----------
    results     : dictionary
        stuff to save
    filename    : str
        name of the file where results are saved
    """
    json_dump(results, filename)

def load_simulation_from_json(filename):
    """
    load results of individual axon simulation from a json. This function is specific to axons simulation as some identified results are automatically converted as numpy arrays.

    Parameters
    ----------
    filename    : str
        name of the file where axons simulations are saved
    """
    results = json_load(filename)
    # convert iterables to numpy arrays
    int_iterables = ['node_index','Markov_Nav_modeled_NoR','V_mem_raster_position','V_mem_filtered_raster_position','V_mem_raster_time_index','V_mem_filtered_raster_time_index']
    for key, value in results.items():
        if is_iterable(value):
            if key in int_iterables:
                results[key] = np.asarray(value,dtype=np.int16)
            else:
                if isinstance(value,str):
                    results[key] = value
                else:
                    results[key] = np.asarray(value,dtype=np.float32)
        else:
            results[key] = value
    return results

##############################################
## HANDLE THE SIMULATION RESULT DICTIONNARY ##
##############################################
def remove_key(my_dict, key):
    """
    Remove an item from a dictionary, usefull before saving files, as some results maybe heavy and are potentially useless after some steps of postprocessing.

    Parameters
    ----------
    my_dict : dictionary
        dictionary where an item should be deleted
    key     : str
        name of the key to delete
    """
    #if isinstance(key, Iterable):
    #    for k in key:
    #        del my_dict[k]
    #else:
    del my_dict[key]
    pass_info('removed the following key from results: ', key)

def remove_non_NoR_zones(my_dict, key):
    """
    Automatically remove values out of nodes of Ranvier for membrane voltage and associated quantities.
    This function is helpfull for large simulation before saving results

    Parameters
    ----------
    my_dict : dictionary
        dictionary where the quantity should be cleaned
    key     : str
        name of the key to clean
    """
    if ('V_mem' in key):
        if my_dict['Axon_type'] == 'Myelinated':
            new_entry = []
            for i in my_dict['Nodes_of_Ranvier_indexes']:
                new_entry.append(my_dict[key][i,:])
            my_dict[key] = np.asarray(new_entry)
        else:
            rise_warning('Warning, remove_non_NoR_zones only applicable to Myelinated axons')
    else:
        rise_warning('Warning, remove_non_NoR_zones only applicable to membrane voltage or current')

############################
## AXON SIGNAL PROCESSING ##
############################
def filter_freq(my_dict,my_key,freq,Q=10):
    """
    Basic Filtering of quantities. This function design a notch filter (scipy IIR-notch).
    Adds an item to the specified dictionary, with the key termination '_filtered' concatenated to the original key.

    Parameters
    ----------
    my_dict : dictionary
        dictionary where the quantity should be filtered
    key     : str
        name of the key to filter
    freq    : float or array, list, np.array
        frequecy or list of frequencies to filter in kHz, as time is defined in ms in NRV2.
        If multiple frequencies, they are filtered sequencially, with as may filters as frequencies, in the specified order
    Q       : float
        quality factor of the filter, by default set to 10
    """
    if isinstance(freq, Iterable):
        f0 = np.asarray(freq)
    else:
        f0 = freq
    if my_dict['dt'] == 0:
        rise_warning('Warning: filtering aborted, variable time step used for differential equation solving')
        return False
    else:
        fs = 1/my_dict['dt']
        if isinstance(f0, Iterable):
            new_sig = np.zeros(my_dict[my_key].shape)
            for k in range(len(my_dict[my_key])):
                new_sig[k,:] = my_dict[my_key][k]
                for f in f0:
                    b_notch, a_notch = signal.iirnotch(f, Q, fs)
                    new_sig[k,:] = signal.lfilter(b_notch,a_notch,new_sig[k,:][k])
        else:
            ##  NOTCH at the stimulation frequency
            b_notch, a_notch = signal.iirnotch(f0, Q, fs)
            new_sig = np.zeros(my_dict[my_key].shape)
            for k in range(len(my_dict[my_key])):
                new_sig[k,:] = signal.lfilter(b_notch,a_notch,my_dict[my_key][k])
        my_dict[my_key+'_filtered'] = new_sig


def rasterize(my_dict,my_key,t_start=0,t_stop=0,t_min_spike=0.1,t_refractory=2,threshold = 0):
    """
    Rasterize a membrane potential (or filtered or any quantity processed from membrane voltage), with spike detection.
    This function adds 4 items to the dictionnary, with the key termination '_raster_position', '_raster_x_position', '_raster_time_index', '_raster_time' concatenated to the original key.
    These keys correspond to:
    _raster_position    : spike position as the indice of the original key
    _raster_x_position  : spike position as geometrical position in um
    _raster_time_index  : spike time as the indice of the original key
    _raster_time        : spike time as ms

    Parameters
    ----------
    my_dict : dictionary
        dictionary where the quantity should be rasterized
    key     : str
        name of the key to rasterize
    t_start         : float
        time at which the spike detection should start, in ms. By default 0
    t_stop          : float
        maximum time to apply spike detection, in ms. If zero is specified, the spike detection is applied to the full signal duration. By default set to 0.
    t_min_spike     : float
        minimum duration of a spike over its threshold, in ms. By default set to 0.1 ms
    t_refractory    : float
        refractory period for a spike, in ms. By default set to 2 ms.
    threshold       : float
        threshold for spike dection, in mV. If 0 is specified the threshold associated with the axon is automatically chosen. By default set to 0.
        Note that if a 0 value is wanted as threshold, a insignificat value (eg. 1e-12) should be specified.
    """
    if t_stop == 0:
        t_stop = int(my_dict['tstop']/my_dict['dt'])
    else:
        t_stop=int(t_stop/my_dict['dt'])
    if threshold == 0:
        thr = my_dict['threshold']
    else:
        thr = threshold
    ## selecting the list of position considering what has been recorded
    if my_dict['myelinated'] == True:
        if my_dict['rec'] == 'all':
            list_to_parse = my_dict['node_index']
            x = my_dict['x']
        else:
            list_to_parse = np.arange(len(my_dict['x_rec']))#my_dict[my_key]
            x = my_dict['x_rec']
    else:
        list_to_parse = np.arange(len(my_dict['x_rec'])) #my_dict[my_key]
        x = my_dict['x_rec']
    # spike detection
    my_dict[my_key+'_raster_position'], my_dict[my_key+'_raster_x_position'], my_dict[my_key+'_raster_time_index'], my_dict[my_key+'_raster_time'] = spike_detection(my_dict[my_key],my_dict['t'],x,list_to_parse, thr, my_dict['dt'], t_start, t_stop, t_refractory, t_min_spike)

@jit(nopython=True,fastmath=True)
def spike_detection(Voltage, t, x, list_to_parse, thr, dt, t_start, t_stop, t_refractory, t_min_spike):
    """
    Internal use only, spike detection just in time compiled to speed up the process
    """
    raster_position=[]
    raster_x_position=[]
    raster_time_index=[]
    raster_time = []
    # parsing to find spikes
    for i in list_to_parse:
        t_last_spike = t_start - t_refractory
        for j in range(int(t_start*(1/dt)),t_stop):
            if Voltage[i][j] <= thr and Voltage[i][j+1] >= thr \
                and Voltage[i][min((j+int(t_min_spike*(1/dt))),t_stop)] >= thr \
                and (j*dt - t_last_spike) > t_refractory: # 1st line: threshold crossing, 2nd: minimum time above threshold,3rd: refractory period
                # there was a spike, get time and position
                raster_position.append(i)
                raster_x_position.append(x[i])
                raster_time_index.append(j)
                raster_time.append(t[j])
                # memorize the time in ms, to evaluate refractory period
                t_last_spike = j*dt
    # return results
    return np.asarray(raster_position), np.asarray(raster_x_position), np.asarray(raster_time_index), np.asarray(raster_time)

def find_spike_origin(my_dict,my_key=None,t_start=0,t_stop=0,x_min=None,x_max=None):
    """
    Find the start time and position of a spike or a spike train. Only work on rasterized keys

    Parameters
    ----------
    my_dict : dictionary
        dictionary where the quantity should be rasterized
    key     : str
        name of the key to consider, if None is specified, the rasterized is automatically chose with preference for filtered-rasterized keys.
    t_start : float
        time at which the spikes are processed, in ms. By default 0
    t_stop  : float
        maximum time at which the spikes are processed, in ms. If zero is specified, the spike detection is applied to the full signal duration. By default set to 0.
    x_min   : float
        minimum position for spike processing, in um. If none is specified, the spike are processed starting at the 0 position. By default set to None.
    x_max   : float
        minimum position for spike processing, in um. If None is specified, spikes are processed on the full axon length . By default set to 0.

    Returns
    -------
    start_time          : float
        first occurance time in ms
    start_x_position    : float
        first occurance position in um
    """
    # define max timing if not already defined
    if t_stop == 0:
        t_stop = my_dict['tstop']
    # find the best raster plot
    if my_key == None:
        if 'V_mem_filtered_raster_position' in my_dict:
            good_key_prefix = 'V_mem_filtered_raster'
        elif 'V_mem_raster_position' in my_dict:
            good_key_prefix = 'V_mem_raster'
        else:
            # there is no rasterized voltage, nothing to find
            return False
    else:
        good_key_prefix = my_key
    # get data only in time windows
    sup_indexes = np.where(my_dict[good_key_prefix+'_time']>t_start)
    inf_indexes = np.where(my_dict[good_key_prefix+'_time']<t_stop)
    good_indexes = np.intersect1d(sup_indexes, inf_indexes)
    good_spike_times = my_dict[good_key_prefix+'_time'][good_indexes]
    good_spike_positions = my_dict[good_key_prefix+'_x_position'][good_indexes]
    # get data only in the z window if applicable
    if x_min != None:
        sup_xmin_indexes = np.where(good_spike_positions>x_min)
    else:
        sup_xmin_indexes = np.arange(len(good_spike_positions))
    if x_max != None:
        inf_xmax_indexes = np.where(good_spike_positions<x_max)
    else:
        inf_xmax_indexes = np.arange(len(good_spike_positions))
    good_x_indexes = np.intersect1d(sup_xmin_indexes,inf_xmax_indexes)
    considered_spike_times = good_spike_times[good_x_indexes]
    considered_spike_positions = good_spike_positions[good_x_indexes]
    # fin the minimum time corresponding to spike initiation
    start_index = np.where(considered_spike_times == np.amin(considered_spike_times))
    start_time = considered_spike_times[start_index]
    start_x_position = considered_spike_positions[start_index]
    return start_time, start_x_position

def find_spike_last_occurance(my_dict,my_key=None,t_start=0,t_stop=0,direction='up',x_start=0):
    """
    Find the last position of a spike occurance for rasterized data

    Parameters
    ----------
    my_dict     : dictionary
        dictionary where the quantity should be rasterized
    key         : str
        name of the key to consider, if None is specified, the rasterized is automatically chose with preference for filtered-rasterized keys.
    t_start     : float
        time at which the spikes are processed, in ms. By default 0
    t_stop      : float
        maximum time at which the spikes are processed, in ms. If zero is specified, the spike detection is applied to the full signal duration. By default set to 0.
    direction   : str
        Direction of the spike propagation, chose between:
            'up'    -> spike propagating to higher x-coordinate values
            'down'  -> spike propagating to lower x-coordinate values
    x_start     : float
        minimum position for spike processing, in um. If None is specified, spikes are processed on the full axon length . By default set to 0.

    Returns
    -------
    t_last      : float
        last occurance time in ms
    x_last  : float
        last occurance position in um
    """
    # define max timing if not already defined
    if t_stop == 0:
        t_stop = my_dict['tstop']
    # find the best raster plot
    if my_key == None:
        if 'V_mem_filtered_raster_position' in my_dict:
            good_key_prefix = 'V_mem_filtered_raster'
        elif 'V_mem_raster_position' in my_dict:
            good_key_prefix = 'V_mem_raster'
        else:
            # there is no rasterized voltage, nothing to find
            return False
    else:
        good_key_prefix = my_key
    # get x_start, eventually t_start
    if x_start == 0:
        t_start, x_start = find_spike_origin(my_dict,my_key=good_key_prefix,t_start=t_start,t_stop=t_stop)
    # get data only in time windows
    sup_indexes = np.where(my_dict[good_key_prefix+'_time']>t_start)
    inf_indexes = np.where(my_dict[good_key_prefix+'_time']<t_stop)
    good_indexes = np.intersect1d(sup_indexes, inf_indexes)
    considered_spike_times = my_dict[good_key_prefix+'_time'][good_indexes]
    considered_spike_positions = my_dict[good_key_prefix+'_x_position'][good_indexes]
    if direction=='up':
        # find the spike in the upper region of the start
        upper_spike_index = np.where(considered_spike_positions > x_start)
        upper_spike_times = considered_spike_times[upper_spike_index]
        upper_spike_positions = considered_spike_positions[upper_spike_index]
        # find the last occurance
        last_spike_index = np.where(upper_spike_times == np.amax(upper_spike_times))
        t_last = upper_spike_times[last_spike_index]
        x_last = upper_spike_positions[last_spike_index]
    elif direction=='down':
        # find the spike in the upper region of the start
        lower_spike_index = np.where(considered_spike_positions < x_start)
        lower_spike_times = considered_spike_times[lower_spike_index]
        lower_spike_positions = considered_spike_positions[lower_spike_index]
        # find the last occurance
        last_spike_index = np.where(lower_spike_times == np.amax(lower_spike_times))
        t_last = lower_spike_times[last_spike_index]
        x_last = lower_spike_positions[last_spike_index]
    else:
        # find the spike in the upper region of the start
        upper_spike_index = np.where(considered_spike_positions > x_start)
        upper_spike_times = considered_spike_times[upper_spike_index]
        upper_spike_positions = considered_spike_positions[upper_spike_index]
        # find the spike in the upper region of the start
        lower_spike_index = np.where(considered_spike_positions < x_start)
        lower_spike_times = considered_spike_times[lower_spike_index]
        lower_spike_positions = considered_spike_positions[lower_spike_index]
        # find the last occurances
        last_upper_spike_index = np.where(upper_spike_times == np.amax(upper_spike_times))
        last_lower_spike_index = np.where(lower_spike_times == np.amax(lower_spike_times))
        t_last = np.asarray([lower_spike_times[last_lower_spike_index][0],lower_spike_times[last_upper_spike_index]][0])
        x_last = np.asarray([lower_spike_positions[last_lower_spike_index][0],lower_spike_positions[last_upper_spike_index]][0])
    return t_last, x_last

def speed(my_dict,position_key=None,t_start=0,t_stop=0,x_start=0,x_stop=0):
    """
    Compute the velocity of a spike from rasterized data in a dictionary. The speed can be either positive or negative depending on the propagation direction.

    Parameters
    ----------
    my_dict     : dictionary
        dictionary where the quantity should be rasterized
    key         : str
        name of the key to consider, if None is specified, the rasterized is automatically chose with preference for filtered-rasterized keys.
    t_start     : float
        time at which the spikes are processed, in ms. By default 0
    t_stop      : float
        maximum time at which the spikes are processed, in ms. If zero is specified, the spike detection is applied to the full signal duration. By default set to 0.
    x_start     : float
        minimum position for spike processing, in um. By default set to 0.
    x_stop      : float
        maximum position for spike processing, in um. If 0 is specified, spikes are processed on the full axon length . By default set to 0.

    Returns
    -------
    speed   : float
        velocity.

    Note
    ----
    the velocity is computed with first and last occurance of a spike, be careful specifying the computation window if multiple spikes. This function will not see velocity variation.
    """
    # define max timing if not already defined
    if t_stop == 0:
        t_stop = my_dict['tstop']
    if t_start==0:
        if 'intra_stim_starts' in my_dict and my_dict['intra_stim_starts']!=[]:
                t_start=my_dict['intra_stim_starts'][0]
    if x_start==0:
        x_stop=my_dict['L']
    elif x_stop==0:
        x_start=my_dict['L']
    # find the best raster plot
    if position_key == None:
        if 'V_mem_filtered_raster_position' in my_dict:
            good_key_prefix = 'V_mem_filtered_raster'
        elif 'V_mem_raster_position' in my_dict:
            good_key_prefix = 'V_mem_raster'
        else:
            # there is no rasterized voltage, nothing to find
            return False
    else:
        good_key_prefix = my_key
    # get data only in time windows
    sup_time_indexes = np.where(my_dict[good_key_prefix+'_time']>t_start)
    inf_time_indexes = np.where(my_dict[good_key_prefix+'_time']<t_stop)
    good_indexes_time = np.intersect1d(sup_time_indexes, inf_time_indexes)
    sup_position_indexes = np.where(my_dict[good_key_prefix+'_x_position'][good_indexes_time]>=x_start)
    inf_position_indexes = np.where(my_dict[good_key_prefix+'_x_position'][good_indexes_time]<=x_stop)
    good_indexes_position = np.intersect1d(sup_position_indexes, inf_position_indexes)
    good_indexes=np.intersect1d(good_indexes_position,good_indexes_time)
    good_spike_times = my_dict[good_key_prefix+'_time'][good_indexes]
    good_spike_positions = my_dict[good_key_prefix+'_x_position'][good_indexes]
    max_time=np.argmax(good_spike_times)
    min_time=np.argmin(good_spike_times)
    speed=(good_spike_positions[max_time]-good_spike_positions[min_time])*10**-3/(good_spike_times[max_time]-good_spike_times[min_time])
    return speed

def block(my_dict,position_key=None,t_start=0,t_stop=0):
    """
    check if an axon is blocked or not. The simulation has to include the test spike. This function will look for the test spike initiation and check the propagation

    Parameters
    ----------
    my_dict     : dictionary
        dictionary where the quantity should be rasterized
    key         : str
        name of the key to consider, if None is specified, the rasterized is automatically chose with preference for filtered-rasterized keys.
    t_start     : float
        time at which the test spikes can occur, in ms. By default 0
    t_stop      : float
        maximum time at which the spikes are processed, in ms. If zero is specified, the spike detection is applied to the full signal duration. By default set to 0.

    Returns
    -------
    flag    : bool or None
        True if the axon is blocked, False if not blocked and None if the test spike does not cross the stimulation near point in the simulation (no possibility to check for the axon state)
    """
    blocked_spike_positionlist=[]
    if t_stop == 0:
        t_stop = my_dict['tstop']
    if t_start==0:
        if 'intra_stim_starts' in my_dict and my_dict['intra_stim_starts']!=[]:
                t_start=my_dict['intra_stim_starts'][0]
    if position_key == None:
        if 'V_mem_filtered_raster_position' in my_dict:
            good_key_prefix = 'V_mem_filtered_raster'
        elif 'V_mem_raster_position' in my_dict:
            good_key_prefix = 'V_mem_raster'
        else:
            # there is no rasterized voltage, nothing to find
            return False
    sup_time_indexes = np.where(my_dict[good_key_prefix+'_time']>t_start)
    inf_time_indexes = np.where(my_dict[good_key_prefix+'_time']<t_stop)
    good_indexes_time = np.intersect1d(sup_time_indexes, inf_time_indexes)
    good_spike_times = my_dict[good_key_prefix+'_time'][good_indexes_time]
    blocked_spike_positionlist = my_dict[good_key_prefix+'_x_position'][good_indexes_time]
    if blocked_spike_positionlist==[]:
        return None
    if 'intra_stim_positions' in my_dict:
        if my_dict['intra_stim_positions']<my_dict['extracellular_electrode_x']:
            if max(blocked_spike_positionlist)<9./10*my_dict['L']:
                return True
            else:
                return False
        else:
            if min(blocked_spike_positionlist)>1./10*my_dict['L']:
                return True
            else:
                return False

@jit(nopython=True,fastmath=True)
def count_spike(onset_position):
    """
    spike counting, just in time compiled. For internal use only.
    """
    if len(onset_position)==0:
        spike_number=0
        return 0
    else:
        spike_number=1
        for i in range(len(onset_position)-1):
            if onset_position[i]==min(onset_position):
                if onset_position[i]==onset_position[i+1]:
                    spike_number=spike_number+1
    return spike_number

#############################
## VISUALIZATION FUNCTIONS ##
#############################
def plot_Nav_states(ax,values,title=''):
    """
    Plot the state machine for kinetic (Markov) Nav 1.1 to 1.9 values

    Parameters:
    -----------
    ax      : matplotlib axis object
        axes of the figure to work on
    values  : list, array, numpy array
    """
    states = ['$I_1$','$I_2$','$C_1$','$C_2$','$O_1$','$O_2$']

    X = [-1,-3,0,1,0,3]
    Y = [0,0,1,0,-1,0]
    c = ['r','r','b','b','g','g']

    ax.set_xlim(-3.4,3.4)
    ax.set_ylim(-1.5,1.5)
    for i in range(len(states)):
        ax.scatter(X[i],Y[i],s=300 + values[i]*1450,c=c[i],alpha=0.4)
        ax.text(X[i],Y[i],states[i],ha='center',va='center')
    # paths
    ax.arrow(-2.5,0.03,1,0,linewidth=1,alpha=0.5,head_width=0.02, head_length=0.02)
    ax.text(-2,0.2,'$I_2I_1$',ha='center',va='center',alpha=0.4)
    ax.arrow(-1.5,-0.03,-1,0,linewidth=1,alpha=0.5,head_width=0.02, head_length=0.02)
    ax.text(-2,-0.2,'$I_1I_2$',ha='center',va='center',alpha=0.4)

    ax.arrow(-0.83,0.25,0.5,0.5,linewidth=1,alpha=0.5,head_width=0.02, head_length=0.02)
    ax.text(-0.9,0.6,'$I_1C_1$',ha='center',va='center',alpha=0.4)
    ax.arrow(-0.22,0.75,-0.5,-0.5,linewidth=1,alpha=0.5,head_width=0.02, head_length=0.02)
    ax.text(-0.45,0.25,'$C_1I_1$',ha='center',va='center',alpha=0.4)

    ax.arrow(0.72,0.25,-0.5,0.5,linewidth=1,alpha=0.5,head_width=0.02, head_length=0.02)
    ax.text(0.9,0.6,'$C_1C_2$',ha='center',va='center',alpha=0.4)
    ax.arrow(0.33,0.75,0.5,-0.5,linewidth=1,alpha=0.5,head_width=0.02, head_length=0.02)
    ax.text(0.45,0.25,'$C_2C_1$',ha='center',va='center',alpha=0.4)

    ax.arrow(0.83,-0.25,-0.5,-0.5,linewidth=1,alpha=0.5,head_width=0.02, head_length=0.02)
    ax.text(0.9,-0.6,'$C_2O_1$',ha='center',va='center',alpha=0.4)
    ax.arrow(0.22,-0.75,0.5,0.5,linewidth=1,alpha=0.5,head_width=0.02, head_length=0.02)
    ax.text(0.45,-0.25,'$O_1C_2$',ha='center',va='center',alpha=0.4)

    ax.arrow(-0.33,-0.75,-0.5,0.5,linewidth=1,alpha=0.5,head_width=0.02, head_length=0.02)
    ax.text(-0.9,-0.6,'$O_1I_1$',ha='center',va='center',alpha=0.4)

    ax.arrow(1.5,0.03,1,0,linewidth=1,alpha=0.5,head_width=0.02, head_length=0.02)
    ax.text(2,0.2,'$C_20_2$',ha='center',va='center',alpha=0.4)
    ax.arrow(2.5,-0.03,-1,0,linewidth=1,alpha=0.5,head_width=0.02, head_length=0.02)
    ax.text(2,-0.2,'$O_2C_2$',ha='center',va='center',alpha=0.4)
    # make axes to disappear
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title(title)
    ax.axis('off')
