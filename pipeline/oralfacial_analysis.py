import numpy as np
import statsmodels.api as sm
import datajoint as dj
import pathlib
from scipy import stats
from scipy import signal
from astropy.stats import kuiper_two
from pipeline import ephys, experiment, tracking, InsertBuffer
from pipeline.ingest import tracking as tracking_ingest

from pipeline.mtl_analysis import helper_functions
from pipeline.plot import behavior_plot
from . import get_schema_name

schema = dj.schema(get_schema_name('oralfacial_analysis'))

v_oralfacial_analysis = dj.create_virtual_module('oralfacial_analysis', get_schema_name('oralfacial_analysis'))
v_tracking = dj.create_virtual_module('tracking', get_schema_name('tracking'))

@schema
class JawTuning(dj.Computed):
    definition = """
    -> ephys.Unit
    ---
    modulation_index: float
    preferred_phase: float
    jaw_x: mediumblob
    jaw_y: mediumblob
    kuiper_test: float
    di_perm: float
    """
    # mtl sessions only
    key_source = experiment.Session & ephys.Unit & tracking.Tracking & 'rig = "RRig-MTL"'
    
    def make(self, key):
        num_frame = 1470
        # get traces and phase
        good_units=ephys.Unit * ephys.ClusterMetric * ephys.UnitStat & key & 'presence_ratio > 0.9' & 'amplitude_cutoff < 0.15' & 'avg_firing_rate > 0.2' & 'isi_violation < 10' & 'unit_amp > 150'
        if len(good_units)==0:
            print(f'No units: {key}')
            return
        
        unit_keys=good_units.fetch('KEY')
        
        miss_trial_side=(v_oralfacial_analysis.BadVideo & key).fetch('miss_trial_side')
        if (miss_trial_side[0] is None):
            miss_trial_side[0]=np.array([0])
        
        traces = tracking.Tracking.JawTracking - [{'trial': tr} for tr in miss_trial_side[0]] & key & {'tracking_device': 'Camera 3'}
        
        if len(ephys.Unit.TrialSpikes - [{'trial': tr} for tr in miss_trial_side[0]] & unit_keys[0]) != len(traces):
            print(f'Mismatch in tracking trial and ephys trial number: {key}')
            return
        
        session_traces = traces.fetch('jaw_y', order_by='trial')
        traces_length = [len(d) for d in session_traces]
        sample_number = int(np.median(traces_length))
        good_trial_ind = np.where(np.array(traces_length) == sample_number)[0]
        good_traces = session_traces[good_trial_ind]
        good_traces = np.vstack(good_traces)
        
        fs=(tracking.TrackingDevice & 'tracking_device="Camera 3"').fetch1('sampling_rate')
        
        amp, phase=behavior_plot.compute_insta_phase_amp(good_traces, float(fs), freq_band=(3, 15))
        phase = phase + np.pi
        phase_s=np.hstack(phase)
        
        # compute phase and MI
        units_jaw_tunings = []
        for unit_key in unit_keys:
            all_spikes=(ephys.Unit.TrialSpikes - [{'trial': tr} for tr in miss_trial_side[0]] & unit_key).fetch('spike_times', order_by='trial')
            good_spikes = np.array(all_spikes[good_trial_ind]*float(fs)) # get good spikes and convert to indices
            good_spikes = [d.astype(int) for d in good_spikes] # convert to intergers
        
            for i, d in enumerate(good_spikes):
                good_spikes[i] = d[d < num_frame]
        
            all_phase = []
            for trial_idx in range(len(good_spikes)):
                all_phase.append(phase[trial_idx][good_spikes[trial_idx]])
        
            all_phase=np.hstack(all_phase)
            
            _, kuiper_test = kuiper_two(phase_s, all_phase)
                        
            n_bins = 20
            tofity, tofitx = np.histogram(all_phase, bins=n_bins)
            baseline, tofitx = np.histogram(phase_s, bins=n_bins)  
            tofitx = tofitx[:-1] + (tofitx[1] - tofitx[0])/2
            tofity = tofity / baseline * float(fs)
                           
            preferred_phase,modulation_index=helper_functions.compute_phase_tuning(tofitx, tofity)
            
            n_perm = 100
            n_spk = len(all_phase)
            di_distr = np.zeros(n_perm)
            for i_perm in range(n_perm):
                tofity_p, _ = np.histogram(np.random.choice(phase_s, n_spk), bins=n_bins) 
                tofity_p = tofity_p / baseline * float(fs)
                _, di_distr[i_perm] = helper_functions.compute_phase_tuning(tofitx, tofity_p)
                
            _, di_perm = stats.mannwhitneyu(modulation_index,di_distr,alternative='greater')          
        
            units_jaw_tunings.append({**unit_key, 'modulation_index': modulation_index, 'preferred_phase': preferred_phase, 'jaw_x': tofitx, 'jaw_y': tofity, 'kuiper_test': kuiper_test, 'di_perm': di_perm})
            
        self.insert(units_jaw_tunings, ignore_extra_fields=True)
        
@schema
class BreathingTuning(dj.Computed):
    definition = """
    -> ephys.Unit
    ---
    modulation_index: float
    preferred_phase: float
    breathing_x: mediumblob
    breathing_y: mediumblob
    """
    # mtl sessions only
    key_source = experiment.Session & experiment.Breathing & ephys.Unit & 'rig = "RRig-MTL"'
    
    def make(self, key):
    
        # get traces and phase
        good_units=ephys.Unit * ephys.ClusterMetric * ephys.UnitStat & key & 'presence_ratio > 0.9' & 'amplitude_cutoff < 0.15' & 'avg_firing_rate > 0.2' & 'isi_violation < 10' & 'unit_amp > 150'
        
        unit_keys=good_units.fetch('KEY')
        
        traces = experiment.Breathing & key
        
        if len(experiment.SessionTrial & (ephys.Unit.TrialSpikes & key)) != len(traces):
            print(f'Mismatch in tracking trial and ephys trial number: {key}')
            return
        
        session_traces, breathing_ts = traces.fetch('breathing', 'breathing_timestamps', order_by='trial')
        fs=25000
        ds=100
        good_traces = session_traces
        for i, d in enumerate(session_traces):
            good_traces[i] = d[breathing_ts[i] < 5][::ds]
        traces_length = [len(d) for d in good_traces]
        good_trial_ind = np.where(np.array(traces_length) == 5*fs/ds)[0]
        good_traces = good_traces[good_trial_ind]
        good_traces = np.vstack(good_traces)
        
        amp, phase=behavior_plot.compute_insta_phase_amp(good_traces, float(fs/ds), freq_band=(1, 15))
        phase = phase + np.pi
        
        # compute phase and MI
        units_breathing_tunings = []
        for unit_key in unit_keys:
            all_spikes=(ephys.Unit.TrialSpikes & unit_key).fetch('spike_times', order_by='trial')
            good_spikes = np.array(all_spikes[good_trial_ind]*float(fs/ds)) # get good spikes and convert to indices
            good_spikes = [d.astype(int) for d in good_spikes] # convert to intergers
        
            for i, d in enumerate(good_spikes):
                good_spikes[i] = d[d < int(5*fs/ds)]
        
            all_phase = []
            for trial_idx in range(len(good_spikes)):
                all_phase.append(phase[trial_idx][good_spikes[trial_idx]])
        
            all_phase=np.hstack(all_phase)
            
            n_bins = 20
            tofity, tofitx = np.histogram(all_phase, bins=n_bins)
            baseline, tofitx = np.histogram(phase, bins=n_bins)  
            tofitx = tofitx[:-1] + (tofitx[1] - tofitx[0])/2
            tofity = tofity / baseline * float(fs/ds)
            
            preferred_phase,modulation_index=helper_functions.compute_phase_tuning(tofitx, tofity)             
        
            units_breathing_tunings.append({**unit_key, 'modulation_index': modulation_index, 'preferred_phase': preferred_phase, 'breathing_x': tofitx, 'breathing_y': tofity})
            
        self.insert(units_breathing_tunings, ignore_extra_fields=True)

@schema
class WhiskerTuning(dj.Computed):
    definition = """
    -> ephys.Unit
    ---
    modulation_index: float
    preferred_phase: float
    whisker_x: mediumblob
    whisker_y: mediumblob
    """
    # mtl sessions only
    key_source = experiment.Session & v_oralfacial_analysis.WhiskerSVD & ephys.Unit & 'rig = "RRig-MTL"'
    
    def make(self, key):
        num_frame = 1471
        # get traces and phase
        good_units=ephys.Unit * ephys.ClusterMetric * ephys.UnitStat & key & 'presence_ratio > 0.9' & 'amplitude_cutoff < 0.15' & 'avg_firing_rate > 0.2' & 'isi_violation < 10' & 'unit_amp > 150'
        
        unit_keys=good_units.fetch('KEY')
        
        traces = tracking.Tracking.JawTracking & key & {'tracking_device': 'Camera 4'}
        
        if len(experiment.SessionTrial & (ephys.Unit.TrialSpikes & key)) != len(traces):
            print(f'Mismatch in tracking trial and ephys trial number: {key}')
            return
        
        session_traces_w = (v_oralfacial_analysis.WhiskerSVD & key).fetch('mot_svd')
        
        if len(session_traces_w[0][:,0]) % num_frame != 0:
            print('Bad videos in bottom view')
            return
        else:
            num_trial_w = int(len(session_traces_w[0][:,0])/num_frame)
            session_traces_w = np.reshape(session_traces_w[0][:,0], (num_trial_w, num_frame))
        trial_idx_nat = [d.astype(str) for d in np.arange(num_trial_w)]
        trial_idx_nat = sorted(range(len(trial_idx_nat)), key=lambda k: trial_idx_nat[k])
        trial_idx_nat = sorted(range(len(trial_idx_nat)), key=lambda k: trial_idx_nat[k])
        session_traces_w=session_traces_w[trial_idx_nat,:]
                                       
        fs=(tracking.TrackingDevice & 'tracking_device="Camera 4"').fetch1('sampling_rate')
        
        amp, phase=behavior_plot.compute_insta_phase_amp(session_traces_w, float(fs), freq_band=(3, 25))
        phase = phase + np.pi
        
        # compute phase and MI
        units_whisker_tunings = []
        for unit_key in unit_keys:
            all_spikes=(ephys.Unit.TrialSpikes & unit_key).fetch('spike_times', order_by='trial')
            good_spikes = np.array(all_spikes*float(fs)) # get good spikes and convert to indices
            good_spikes = [d.astype(int) for d in good_spikes] # convert to intergers
        
            for i, d in enumerate(good_spikes):
                good_spikes[i] = d[d < int(5*fs)]
        
            all_phase = []
            for trial_idx in range(len(good_spikes)):
                all_phase.append(phase[trial_idx][good_spikes[trial_idx]])
        
            all_phase=np.hstack(all_phase)
            
            n_bins = 20
            tofity, tofitx = np.histogram(all_phase, bins=n_bins)
            baseline, tofitx = np.histogram(phase, bins=n_bins)  
            tofitx = tofitx[:-1] + (tofitx[1] - tofitx[0])/2
            tofity = tofity / baseline * float(fs)
            
            #print(unit_key)
            preferred_phase,modulation_index=helper_functions.compute_phase_tuning(tofitx, tofity)             
        
            units_whisker_tunings.append({**unit_key, 'modulation_index': modulation_index, 'preferred_phase': preferred_phase, 'whisker_x': tofitx, 'whisker_y': tofity})
            
        self.insert(units_whisker_tunings, ignore_extra_fields=True)

@schema
class GLMFit(dj.Computed):
    definition = """
    -> ephys.Unit
    ---
    r2: mediumblob
    r2_t: mediumblob
    weights: mediumblob
    test_y: longblob
    predict_y: longblob
    test_x: longblob
    """
    # mtl sessions only
    key_source = experiment.Session & v_tracking.TongueTracking3DBot & experiment.Breathing & v_oralfacial_analysis.WhiskerSVD & ephys.Unit & 'rig = "RRig-MTL"'
    
    def make(self, key):
        num_frame = 1471
        good_units=ephys.Unit * ephys.ClusterMetric * ephys.UnitStat & key & 'presence_ratio > 0.9' & 'amplitude_cutoff < 0.15' & 'avg_firing_rate > 0.2' & 'isi_violation < 10' & 'unit_amp > 150'
        unit_keys=good_units.fetch('KEY')
        bin_width = 0.017

        bad_trial_side,bad_trial_bot,miss_trial_side,miss_trial_bot=(v_oralfacial_analysis.BadVideo & key).fetch('bad_trial_side','bad_trial_bot','miss_trial_side','miss_trial_bot')
        if (bad_trial_side[0] is None):
            bad_trial_side[0]=np.array([0])
        if (miss_trial_side[0] is None):
            miss_trial_side[0]=np.array([0])
        if (bad_trial_bot[0] is None):
            bad_trial_bot[0]=np.array([0])
        if (miss_trial_bot[0] is None):
            miss_trial_bot[0]=np.array([0])    

        bad_trials=np.concatenate((bad_trial_side[0],bad_trial_bot[0],miss_trial_side[0],miss_trial_bot[0]))
        # from the cameras
        tongue_thr = 0.95
        traces_s = tracking.Tracking.TongueTracking - [{'trial': tr} for tr in bad_trials] & key & {'tracking_device': 'Camera 3'} 
        traces_b = tracking.Tracking.TongueTracking - [{'trial': tr} for tr in bad_trials] & key & {'tracking_device': 'Camera 4'}

        if len(ephys.Unit.TrialSpikes - [{'trial': tr} for tr in bad_trials] & unit_keys[0]) != len(traces_s):
            print(f'Mismatch in tracking trial and ephys trial number: {key}')
            # return
        if len(ephys.Unit.TrialSpikes - [{'trial': tr} for tr in bad_trials] & unit_keys[0]) != len(traces_b):
            print(f'Mismatch in tracking trial and ephys trial number: {key}')
            # return

        trial_key_o=(v_tracking.TongueTracking3DBot - [{'trial': tr} for tr in bad_trials] & key).fetch('trial', order_by='trial')
        traces_s = tracking.Tracking.TongueTracking - [{'trial': tr} for tr in bad_trials] & key & {'tracking_device': 'Camera 3'} & [{'trial': tr} for tr in trial_key_o]
        traces_b = tracking.Tracking.TongueTracking - [{'trial': tr} for tr in bad_trials] & key & {'tracking_device': 'Camera 4'} & [{'trial': tr} for tr in trial_key_o]

        session_traces_s_l_o = traces_s.fetch('tongue_likelihood', order_by='trial')
        session_traces_b_l_o = traces_b.fetch('tongue_likelihood', order_by='trial')

        test_t_o = trial_key_o[::5] # test trials
        _,_,test_t=np.intersect1d(test_t_o,trial_key_o,return_indices=True)
        test_t=test_t+1
        trial_key=np.setdiff1d(trial_key_o,test_t_o)
        _,_,trial_key=np.intersect1d(trial_key,trial_key_o,return_indices=True)
        trial_key=trial_key+1

        session_traces_s_l = session_traces_s_l_o[trial_key-1]
        session_traces_b_l = session_traces_b_l_o[trial_key-1]
        session_traces_s_l = np.vstack(session_traces_s_l)
        session_traces_b_l = np.vstack(session_traces_b_l)
        session_traces_t_l = session_traces_b_l
        session_traces_t_l[np.where((session_traces_s_l > tongue_thr) & (session_traces_b_l > tongue_thr))] = 1
        session_traces_t_l[np.where((session_traces_s_l <= tongue_thr) | (session_traces_b_l <= tongue_thr))] = 0
        session_traces_t_l = np.hstack(session_traces_t_l)

        session_traces_s_l_t = session_traces_s_l_o[test_t-1]
        session_traces_b_l_t = session_traces_b_l_o[test_t-1]
        session_traces_s_l_t = np.vstack(session_traces_s_l_t)
        session_traces_b_l_t = np.vstack(session_traces_b_l_t)
        session_traces_t_l_t = session_traces_b_l_t
        session_traces_t_l_t[np.where((session_traces_s_l_t > tongue_thr) & (session_traces_b_l_t > tongue_thr))] = 1
        session_traces_t_l_t[np.where((session_traces_s_l_t <= tongue_thr) | (session_traces_b_l_t <= tongue_thr))] = 0
        session_traces_t_l_t = np.hstack(session_traces_t_l_t)

        session_traces_s_l_f = np.vstack(session_traces_s_l_o)
        session_traces_b_l_f = np.vstack(session_traces_b_l_o)
        session_traces_t_l_f = session_traces_b_l_f
        session_traces_t_l_f[np.where((session_traces_s_l_f > tongue_thr) & (session_traces_b_l_f > tongue_thr))] = 1
        session_traces_t_l_f[np.where((session_traces_s_l_f <= tongue_thr) | (session_traces_b_l_f <= tongue_thr))] = 0

        # from 3D calibration
        traces_s = v_tracking.JawTracking3DSid & key & [{'trial': tr} for tr in trial_key_o]
        traces_b = v_tracking.TongueTracking3DBot & key & [{'trial': tr} for tr in trial_key_o]
        session_traces_s_y_o, session_traces_s_x_o, session_traces_s_z_o = traces_s.fetch('jaw_y', 'jaw_x', 'jaw_z', order_by='trial')
        session_traces_b_y_o, session_traces_b_x_o, session_traces_b_z_o = traces_b.fetch('tongue_y', 'tongue_x', 'tongue_z', order_by='trial')
        session_traces_s_y_o = stats.zscore(np.vstack(session_traces_s_y_o),axis=None)
        session_traces_s_x_o = stats.zscore(np.vstack(session_traces_s_x_o),axis=None)
        session_traces_s_z_o = stats.zscore(np.vstack(session_traces_s_z_o),axis=None)
        session_traces_b_y_o = np.vstack(session_traces_b_y_o)
        traces_y_mean=np.mean(session_traces_b_y_o[np.where(session_traces_t_l_f == 1)])
        traces_y_std=np.std(session_traces_b_y_o[np.where(session_traces_t_l_f == 1)])
        session_traces_b_y_o = (session_traces_b_y_o - traces_y_mean)/traces_y_std
        session_traces_b_x_o = np.vstack(session_traces_b_x_o)
        traces_x_mean=np.mean(session_traces_b_x_o[np.where(session_traces_t_l_f == 1)])
        traces_x_std=np.std(session_traces_b_x_o[np.where(session_traces_t_l_f == 1)])
        session_traces_b_x_o = (session_traces_b_x_o - traces_x_mean)/traces_x_std
        session_traces_b_z_o = np.vstack(session_traces_b_z_o)
        traces_z_mean=np.mean(session_traces_b_z_o[np.where(session_traces_t_l_f == 1)])
        traces_z_std=np.std(session_traces_b_z_o[np.where(session_traces_t_l_f == 1)])
        session_traces_b_z_o = (session_traces_b_z_o - traces_z_mean)/traces_z_std

        session_traces_s_y = session_traces_s_y_o[trial_key-1]
        session_traces_s_x = session_traces_s_x_o[trial_key-1]
        session_traces_s_z = session_traces_s_z_o[trial_key-1]
        session_traces_b_y = session_traces_b_y_o[trial_key-1]
        session_traces_b_x = session_traces_b_x_o[trial_key-1]
        session_traces_b_z = session_traces_b_z_o[trial_key-1]
        traces_len = np.size(session_traces_b_z, axis = 1)
        num_trial = np.size(session_traces_b_z, axis = 0)

        # format the video data
        session_traces_s_y = np.hstack(session_traces_s_y)
        session_traces_s_x = np.hstack(session_traces_s_x)
        session_traces_s_z = np.hstack(session_traces_s_z)
        session_traces_b_y = np.hstack(session_traces_b_y)
        session_traces_b_x = np.hstack(session_traces_b_x)
        session_traces_b_z = np.hstack(session_traces_b_z)
        # -- moving-average and down-sample
        window_size = int(bin_width/0.0034)  # sample
        kernel = np.ones(window_size) / window_size
        session_traces_s_x = np.convolve(session_traces_s_x, kernel, 'same')
        session_traces_s_x = session_traces_s_x[window_size::window_size]
        session_traces_s_y = np.convolve(session_traces_s_y, kernel, 'same')
        session_traces_s_y = session_traces_s_y[window_size::window_size]
        session_traces_s_z = np.convolve(session_traces_s_z, kernel, 'same')
        session_traces_s_z = session_traces_s_z[window_size::window_size]
        session_traces_b_x = np.convolve(session_traces_b_x, kernel, 'same')
        session_traces_b_x = session_traces_b_x[window_size::window_size]
        session_traces_b_y = np.convolve(session_traces_b_y, kernel, 'same')
        session_traces_b_y = session_traces_b_y[window_size::window_size]
        session_traces_b_z = np.convolve(session_traces_b_z, kernel, 'same')
        session_traces_b_z = session_traces_b_z[window_size::window_size]
        session_traces_t_l = np.convolve(session_traces_t_l, kernel, 'same')
        session_traces_t_l = session_traces_t_l[window_size::window_size]
        session_traces_t_l[np.where(session_traces_t_l < 1)] = 0
        session_traces_s_x = np.reshape(session_traces_s_x,(-1,1))
        session_traces_s_y = np.reshape(session_traces_s_y,(-1,1))
        session_traces_s_z = np.reshape(session_traces_s_z,(-1,1))
        session_traces_b_x = np.reshape(session_traces_b_x * session_traces_t_l, (-1,1))
        session_traces_b_y = np.reshape(session_traces_b_y * session_traces_t_l, (-1,1))
        session_traces_b_z = np.reshape(session_traces_b_z * session_traces_t_l, (-1,1))

        # test trials
        session_traces_s_y_t = session_traces_s_y_o[test_t-1]
        session_traces_s_x_t = session_traces_s_x_o[test_t-1]
        session_traces_s_z_t = session_traces_s_z_o[test_t-1]
        session_traces_b_y_t = session_traces_b_y_o[test_t-1]
        session_traces_b_x_t = session_traces_b_x_o[test_t-1]
        session_traces_b_z_t = session_traces_b_z_o[test_t-1]
        traces_len_t = np.size(session_traces_b_z_t, axis = 1)
        num_trial_t = np.size(session_traces_b_z_t, axis = 0)

        session_traces_s_y_t = np.hstack(session_traces_s_y_t)
        session_traces_s_x_t = np.hstack(session_traces_s_x_t)
        session_traces_s_z_t = np.hstack(session_traces_s_z_t)
        session_traces_b_y_t = np.hstack(session_traces_b_y_t)
        session_traces_b_x_t = np.hstack(session_traces_b_x_t)
        session_traces_b_z_t = np.hstack(session_traces_b_z_t)
        # -- moving-average and down-sample
        session_traces_s_x_t = np.convolve(session_traces_s_x_t, kernel, 'same')
        session_traces_s_x_t = session_traces_s_x_t[window_size::window_size]
        session_traces_s_y_t = np.convolve(session_traces_s_y_t, kernel, 'same')
        session_traces_s_y_t = session_traces_s_y_t[window_size::window_size]
        session_traces_s_z_t = np.convolve(session_traces_s_z_t, kernel, 'same')
        session_traces_s_z_t = session_traces_s_z_t[window_size::window_size]
        session_traces_b_x_t = np.convolve(session_traces_b_x_t, kernel, 'same')
        session_traces_b_x_t = session_traces_b_x_t[window_size::window_size]
        session_traces_b_y_t = np.convolve(session_traces_b_y_t, kernel, 'same')
        session_traces_b_y_t = session_traces_b_y_t[window_size::window_size]
        session_traces_b_z_t = np.convolve(session_traces_b_z_t, kernel, 'same')
        session_traces_b_z_t = session_traces_b_z_t[window_size::window_size]
        session_traces_t_l_t = np.convolve(session_traces_t_l_t, kernel, 'same')
        session_traces_t_l_t = session_traces_t_l_t[window_size::window_size]
        session_traces_t_l_t[np.where(session_traces_t_l_t < 1)] = 0
        session_traces_s_x_t = np.reshape(session_traces_s_x_t,(-1,1))
        session_traces_s_y_t = np.reshape(session_traces_s_y_t,(-1,1))
        session_traces_s_z_t = np.reshape(session_traces_s_z_t,(-1,1))
        session_traces_b_x_t = np.reshape(session_traces_b_x_t * session_traces_t_l_t, (-1,1))
        session_traces_b_y_t = np.reshape(session_traces_b_y_t * session_traces_t_l_t, (-1,1))
        session_traces_b_z_t = np.reshape(session_traces_b_z_t * session_traces_t_l_t, (-1,1))

        # get breathing
        breathing, breathing_ts = (experiment.Breathing - [{'trial': tr} for tr in bad_trials] & key & [{'trial': tr} for tr in trial_key_o]).fetch('breathing', 'breathing_timestamps', order_by='trial')
        good_breathing = breathing
        for i, d in enumerate(breathing):
            good_breathing[i] = d[breathing_ts[i] < traces_len*3.4/1000]
        good_breathing_o = stats.zscore(np.vstack(good_breathing),axis=None)

        good_breathing = np.hstack(good_breathing_o[trial_key-1])
        # -- moving-average
        window_size = int(round(bin_width/(breathing_ts[0][1]-breathing_ts[0][0]),0))  # sample
        kernel = np.ones(window_size) / window_size
        good_breathing = np.convolve(good_breathing, kernel, 'same')
        # -- down-sample
        good_breathing = good_breathing[window_size::window_size]
        good_breathing = np.reshape(good_breathing,(-1,1))

        # test trials
        good_breathing_t = np.hstack(good_breathing_o[test_t-1])
        # -- moving-average
        good_breathing_t = np.convolve(good_breathing_t, kernel, 'same')
        # -- down-sample
        good_breathing_t = good_breathing_t[window_size::window_size]
        good_breathing_t = np.reshape(good_breathing_t,(-1,1))

        # get whisker
        session_traces_w = (v_oralfacial_analysis.WhiskerSVD & key).fetch('mot_svd')
        if len(session_traces_w[0][:,0]) % num_frame != 0:
            print('Bad videos in bottom view')
            return
        else:
            num_trial_w = int(len(session_traces_w[0][:,0])/num_frame)
            session_traces_w = np.reshape(session_traces_w[0][:,0], (num_trial_w, num_frame))
            
        trial_idx_nat = [d.astype(str) for d in np.arange(num_trial_w)]
        trial_idx_nat = sorted(range(len(trial_idx_nat)), key=lambda k: trial_idx_nat[k])
        trial_idx_nat = sorted(range(len(trial_idx_nat)), key=lambda k: trial_idx_nat[k])
        session_traces_w = session_traces_w[trial_idx_nat,:]
        session_traces_w_o = stats.zscore(session_traces_w,axis=None)
        session_traces_w_o = session_traces_w_o[trial_key_o-1]

        session_traces_w = session_traces_w_o[trial_key-1,:]
        session_traces_w = np.hstack(session_traces_w)
        window_size = int(bin_width/0.0034)  # sample
        kernel = np.ones(window_size) / window_size
        session_traces_w = np.convolve(session_traces_w, kernel, 'same')
        session_traces_w = session_traces_w[window_size::window_size]
        session_traces_w = np.reshape(session_traces_w,(-1,1))

        session_traces_w_t = session_traces_w_o[test_t-1,:]
        session_traces_w_t = np.hstack(session_traces_w_t)
        session_traces_w_t = np.convolve(session_traces_w_t, kernel, 'same')
        session_traces_w_t = session_traces_w_t[window_size::window_size]
        session_traces_w_t = np.reshape(session_traces_w_t,(-1,1))

        # stimulus
        V_design_matrix = np.concatenate((session_traces_s_x, session_traces_s_y, session_traces_s_z, session_traces_b_x, session_traces_b_y, session_traces_b_z, good_breathing, session_traces_w), axis=1)
        V_design_matrix_t = np.concatenate((session_traces_s_x_t, session_traces_s_y_t, session_traces_s_z_t, session_traces_b_x_t, session_traces_b_y_t, session_traces_b_z_t, good_breathing_t, session_traces_w_t), axis=1)

        #set up GLM
        sm_log_Link = sm.genmod.families.links.log

        taus = np.arange(-5,6)

        units_glm = []

        for unit_key in unit_keys: # loop for each neuron
            all_spikes=(ephys.Unit.TrialSpikes - [{'trial': tr} for tr in bad_trials] & unit_key & [{'trial': tr} for tr in trial_key_o]).fetch('spike_times', order_by='trial')
            
            good_spikes = np.array(all_spikes[trial_key-1]) # get good spikes
            for i, d in enumerate(good_spikes):
                good_spikes[i] = d[d < traces_len*3.4/1000]+traces_len*3.4/1000*i
            good_spikes = np.hstack(good_spikes)          
            y, bin_edges = np.histogram(good_spikes, np.arange(0, traces_len*3.4/1000*num_trial, bin_width))
            
            good_spikes_t = np.array(all_spikes[test_t-1]) # get good spikes
            for i, d in enumerate(good_spikes_t):
                good_spikes_t[i] = d[d < traces_len_t*3.4/1000]+traces_len_t*3.4/1000*i
            good_spikes_t = np.hstack(good_spikes_t)
            y_t, bin_edges = np.histogram(good_spikes_t, np.arange(0, traces_len_t*3.4/1000*num_trial_t, bin_width))
            
            r2s=np.zeros(len(taus))
            r2s_t=r2s
            weights_t=np.zeros((len(taus),9))
            predict_ys=np.zeros((len(taus),len(y_t)))
            for i, tau in enumerate(taus):
                y_roll=np.roll(y,tau)
                y_roll_t=np.roll(y_t,tau)
                glm_poiss = sm.GLM(y_roll, sm.add_constant(V_design_matrix), family=sm.families.Poisson(link=sm_log_Link))
            
                try:
                    glm_result = glm_poiss.fit()                   
                    sst_val = sum(map(lambda x: np.power(x,2),y_roll-np.mean(y_roll))) 
                    sse_val = sum(map(lambda x: np.power(x,2),glm_result.resid_response))
                    r2s[i] = 1.0 - sse_val/sst_val
                    
                    y_roll_t_p=glm_result.predict(sm.add_constant(V_design_matrix_t))
                    sst_val = sum(map(lambda x: np.power(x,2),y_roll_t-np.mean(y_roll_t))) 
                    sse_val = sum(map(lambda x: np.power(x,2),y_roll_t-y_roll_t_p)) 
                    r2s_t[i] = 1.0 - sse_val/sst_val
                    predict_ys[i,:]=y_roll_t_p
                    weights_t[i,:] = glm_result.params
                    
                except:
                    pass
                
            units_glm.append({**unit_key, 'r2': r2s, 'r2_t': r2s_t, 'weights': weights_t, 'test_y': y_t, 'predict_y': predict_ys, 'test_x': V_design_matrix_t})
            print(unit_key)
            
        self.insert(units_glm, ignore_extra_fields=True)
        
@schema
class GLMFitNoLick(dj.Computed):
    definition = """
    -> ephys.Unit
    ---
    r2_nolick: mediumblob
    weights_nolick: mediumblob
    y_nolick: longblob
    predict_y_nolick: longblob
    x_nolick: longblob
    """
    # mtl sessions only
    key_source = experiment.Session & v_tracking.TongueTracking3DBot & experiment.Breathing & v_oralfacial_analysis.WhiskerSVD & ephys.Unit & 'rig = "RRig-MTL"'
    
    def make(self, key):
        good_units=ephys.Unit * ephys.ClusterMetric * ephys.UnitStat & key & 'presence_ratio > 0.9' & 'amplitude_cutoff < 0.15' & 'avg_firing_rate > 0.2' & 'isi_violation < 10' & 'unit_amp > 150'
        unit_keys=good_units.fetch('KEY')
        bin_width = 0.017
        
        bad_trial_side,bad_trial_bot,miss_trial_side,miss_trial_bot=(v_oralfacial_analysis.BadVideo & key).fetch('bad_trial_side','bad_trial_bot','miss_trial_side','miss_trial_bot')
        if (bad_trial_side[0] is None):
            bad_trial_side[0]=np.array([0])
        if (miss_trial_side[0] is None):
            miss_trial_side[0]=np.array([0])
        if (bad_trial_bot[0] is None):
            bad_trial_bot[0]=np.array([0])
        if (miss_trial_bot[0] is None):
            miss_trial_bot[0]=np.array([0])    
        bad_trials=np.concatenate((bad_trial_side[0],bad_trial_bot[0],miss_trial_side[0],miss_trial_bot[0]))

        # from the cameras
        tongue_thr = 0.95
        traces_s = tracking.Tracking.TongueTracking - [{'trial': tr} for tr in bad_trials] & key & {'tracking_device': 'Camera 3'} 
        traces_b = tracking.Tracking.TongueTracking - [{'trial': tr} for tr in bad_trials] & key & {'tracking_device': 'Camera 4'}
        
        if len(ephys.Unit.TrialSpikes - [{'trial': tr} for tr in bad_trials] & unit_keys[0]) != len(traces_s):
            print(f'Mismatch in tracking trial and ephys trial number: {key}')
            return
        if len(ephys.Unit.TrialSpikes - [{'trial': tr} for tr in bad_trials] & unit_keys[0]) != len(traces_b):
            print(f'Mismatch in tracking trial and ephys trial number: {key}')
            return
        
        # from the cameras
        tongue_thr = 0.95
        trial_key=(v_tracking.TongueTracking3DBot - [{'trial': tr} for tr in bad_trials] & key).fetch('trial', order_by='trial')
        traces_s = tracking.Tracking.TongueTracking - [{'trial': tr} for tr in bad_trials] & key & {'tracking_device': 'Camera 3'} & [{'trial': tr} for tr in trial_key]
        traces_b = tracking.Tracking.TongueTracking - [{'trial': tr} for tr in bad_trials] & key & {'tracking_device': 'Camera 4'} & [{'trial': tr} for tr in trial_key]
        session_traces_s_l = traces_s.fetch('tongue_likelihood', order_by='trial')
        session_traces_b_l = traces_b.fetch('tongue_likelihood', order_by='trial')

        session_traces_s_l = np.vstack(session_traces_s_l)
        session_traces_b_l = np.vstack(session_traces_b_l)
        session_traces_t_l = session_traces_b_l
        session_traces_t_l[np.where((session_traces_s_l > tongue_thr) & (session_traces_b_l > tongue_thr))] = 1
        session_traces_t_l[np.where((session_traces_s_l <= tongue_thr) | (session_traces_b_l <= tongue_thr))] = 0
        session_traces_t_l = np.hstack(session_traces_t_l)

        session_traces_s_l_f = np.vstack(session_traces_s_l)
        session_traces_b_l_f = np.vstack(session_traces_b_l)
        session_traces_t_l_f = session_traces_b_l_f
        session_traces_t_l_f[np.where((session_traces_s_l_f > tongue_thr) & (session_traces_b_l_f > tongue_thr))] = 1
        session_traces_t_l_f[np.where((session_traces_s_l_f <= tongue_thr) | (session_traces_b_l_f <= tongue_thr))] = 0

        # from 3D calibration
        traces_s = v_tracking.JawTracking3DSid & key & [{'trial': tr} for tr in trial_key]
        traces_b = v_tracking.TongueTracking3DBot & key & [{'trial': tr} for tr in trial_key]
        session_traces_s_y, session_traces_s_x, session_traces_s_z = traces_s.fetch('jaw_y', 'jaw_x', 'jaw_z', order_by='trial')
        session_traces_b_y, session_traces_b_x, session_traces_b_z = traces_b.fetch('tongue_y', 'tongue_x', 'tongue_z', order_by='trial')
        session_traces_s_y = stats.zscore(np.vstack(session_traces_s_y),axis=None)
        session_traces_s_x = stats.zscore(np.vstack(session_traces_s_x),axis=None)
        session_traces_s_z = stats.zscore(np.vstack(session_traces_s_z),axis=None)
        session_traces_b_y = np.vstack(session_traces_b_y)
        traces_y_mean=np.mean(session_traces_b_y[np.where(session_traces_t_l_f == 1)])
        traces_y_std=np.std(session_traces_b_y[np.where(session_traces_t_l_f == 1)])
        session_traces_b_y = (session_traces_b_y - traces_y_mean)/traces_y_std
        session_traces_b_x = np.vstack(session_traces_b_x)
        traces_x_mean=np.mean(session_traces_b_x[np.where(session_traces_t_l_f == 1)])
        traces_x_std=np.std(session_traces_b_x[np.where(session_traces_t_l_f == 1)])
        session_traces_b_x = (session_traces_b_x - traces_x_mean)/traces_x_std
        session_traces_b_z = np.vstack(session_traces_b_z)
        traces_z_mean=np.mean(session_traces_b_z[np.where(session_traces_t_l_f == 1)])
        traces_z_std=np.std(session_traces_b_z[np.where(session_traces_t_l_f == 1)])
        session_traces_b_z = (session_traces_b_z - traces_z_mean)/traces_z_std

        traces_len = np.size(session_traces_b_z, axis = 1)
        num_trial = np.size(session_traces_b_z, axis = 0)

        # format the video data
        session_traces_s_y = np.hstack(session_traces_s_y)
        session_traces_s_x = np.hstack(session_traces_s_x)
        session_traces_s_z = np.hstack(session_traces_s_z)
        session_traces_b_y = np.hstack(session_traces_b_y)
        session_traces_b_x = np.hstack(session_traces_b_x)
        session_traces_b_z = np.hstack(session_traces_b_z)
        # -- moving-average and down-sample
        window_size = int(bin_width/0.0034)  # sample
        kernel = np.ones(window_size) / window_size
        session_traces_s_x = np.convolve(session_traces_s_x, kernel, 'same')
        session_traces_s_x = session_traces_s_x[window_size::window_size]
        session_traces_s_y = np.convolve(session_traces_s_y, kernel, 'same')
        session_traces_s_y = session_traces_s_y[window_size::window_size]
        session_traces_s_z = np.convolve(session_traces_s_z, kernel, 'same')
        session_traces_s_z = session_traces_s_z[window_size::window_size]
        session_traces_b_x = np.convolve(session_traces_b_x, kernel, 'same')
        session_traces_b_x = session_traces_b_x[window_size::window_size]
        session_traces_b_y = np.convolve(session_traces_b_y, kernel, 'same')
        session_traces_b_y = session_traces_b_y[window_size::window_size]
        session_traces_b_z = np.convolve(session_traces_b_z, kernel, 'same')
        session_traces_b_z = session_traces_b_z[window_size::window_size]
        session_traces_t_l = np.convolve(session_traces_t_l, kernel, 'same')
        session_traces_t_l = session_traces_t_l[window_size::window_size]
        session_traces_t_l[np.where(session_traces_t_l < 1)] = 0
        session_traces_s_x = np.reshape(session_traces_s_x,(-1,1))
        session_traces_s_y = np.reshape(session_traces_s_y,(-1,1))
        session_traces_s_z = np.reshape(session_traces_s_z,(-1,1))
        session_traces_b_x = np.reshape(session_traces_b_x * session_traces_t_l, (-1,1))
        session_traces_b_y = np.reshape(session_traces_b_y * session_traces_t_l, (-1,1))
        session_traces_b_z = np.reshape(session_traces_b_z * session_traces_t_l, (-1,1))

        # get breathing
        breathing, breathing_ts = (experiment.Breathing - [{'trial': tr} for tr in bad_trials] & key & [{'trial': tr} for tr in trial_key]).fetch('breathing', 'breathing_timestamps', order_by='trial')
        good_breathing = breathing
        for i, d in enumerate(breathing):
            good_breathing[i] = d[breathing_ts[i] < traces_len*3.4/1000]
        good_breathing = stats.zscore(np.vstack(good_breathing),axis=None)

        good_breathing = np.hstack(good_breathing)
        # -- moving-average
        window_size = int(round(bin_width/(breathing_ts[0][1]-breathing_ts[0][0]),0))  # sample
        kernel = np.ones(window_size) / window_size
        good_breathing = np.convolve(good_breathing, kernel, 'same')
        # -- down-sample
        good_breathing = good_breathing[window_size::window_size]
        good_breathing = np.reshape(good_breathing,(-1,1))

        # get whisker
        session_traces_w = (v_oralfacial_analysis.WhiskerSVD & key).fetch('mot_svd')
        if len(session_traces_w[0][:,0]) % 1471 != 0:
            print('Bad videos in bottom view')
            return
        else:
            num_trial_w = int(len(session_traces_w[0][:,0])/1471)
            session_traces_w = np.reshape(session_traces_w[0][:,0], (num_trial_w, 1471))
            
        trial_idx_nat = [d.astype(str) for d in np.arange(num_trial_w)]
        trial_idx_nat = sorted(range(len(trial_idx_nat)), key=lambda k: trial_idx_nat[k])
        trial_idx_nat = sorted(range(len(trial_idx_nat)), key=lambda k: trial_idx_nat[k])
        session_traces_w = session_traces_w[trial_idx_nat,:]
        session_traces_w_o = stats.zscore(session_traces_w,axis=None)
        session_traces_w = session_traces_w_o[trial_key-1]
           
        session_traces_w = np.hstack(session_traces_w)
        window_size = int(bin_width/0.0034)  # sample
        kernel = np.ones(window_size) / window_size
        session_traces_w = np.convolve(session_traces_w, kernel, 'same')
        session_traces_w = session_traces_w[window_size::window_size]
        session_traces_w = np.reshape(session_traces_w,(-1,1))

        # stimulus
        lick_onset_time,lick_offset_time=(v_oralfacial_analysis.MovementTiming & key).fetch1('lick_onset','lick_offset')

        all_period_idx=np.arange(len(session_traces_b_y))
        good_period_idx=[all_period_idx[(all_period_idx*bin_width<lick_onset_time[1]-0.2)]] # restrict by whisking bouts
        for i,val in enumerate(lick_onset_time[1:]):
            good_period_idx.append(all_period_idx[(all_period_idx*bin_width<lick_onset_time[i+1]-0.2) & (all_period_idx*bin_width>lick_offset_time[i]+0.2)])
        good_period_idx.append(all_period_idx[(all_period_idx*bin_width>lick_offset_time[-1]+0.2)])
        good_period_idx=np.array(good_period_idx)
        good_period_idx=np.hstack(good_period_idx)

        session_traces_s_x=stats.zscore(session_traces_s_x[good_period_idx])
        session_traces_s_y=stats.zscore(session_traces_s_y[good_period_idx])
        session_traces_s_z=stats.zscore(session_traces_s_z[good_period_idx])
        session_traces_b_x=session_traces_b_x[good_period_idx]
        traces_x_mean=np.mean(session_traces_b_x[session_traces_b_x != 0])
        traces_x_std=np.std(session_traces_b_x[session_traces_b_x != 0])
        session_traces_b_x = (session_traces_b_x - traces_x_mean)/traces_x_std
        session_traces_b_y=session_traces_b_y[good_period_idx]
        traces_y_mean=np.mean(session_traces_b_y[session_traces_b_y != 0])
        traces_y_std=np.std(session_traces_b_y[session_traces_b_y != 0])
        session_traces_b_y = (session_traces_b_y - traces_y_mean)/traces_y_std
        session_traces_b_z=session_traces_b_z[good_period_idx]
        traces_z_mean=np.mean(session_traces_b_z[session_traces_b_z != 0])
        traces_z_std=np.std(session_traces_b_z[session_traces_b_z != 0])
        session_traces_b_z = (session_traces_b_z - traces_z_mean)/traces_z_std
        good_breathing=stats.zscore(good_breathing[good_period_idx])
        session_traces_w=stats.zscore(session_traces_w[good_period_idx])

        V_design_matrix = np.concatenate((session_traces_s_x, session_traces_s_y, session_traces_s_z, session_traces_b_x, session_traces_b_y, session_traces_b_z, good_breathing, session_traces_w), axis=1)
        
        #set up GLM
        sm_log_Link = sm.genmod.families.links.log

        taus = np.arange(-5,6)

        #units_glm = []
        
        with InsertBuffer(self, 10, skip_duplicates=True, ignore_extra_fields=True, allow_direct_insert=True) as ib:
            
            for unit_key in unit_keys: # loop for each neuron
    
                all_spikes=(ephys.Unit.TrialSpikes - [{'trial': tr} for tr in bad_trials] & unit_key & [{'trial': tr} for tr in trial_key]).fetch('spike_times', order_by='trial')
                
                good_spikes =all_spikes # get good spikes
                for i, d in enumerate(good_spikes):
                    good_spikes[i] = d[d < traces_len*3.4/1000]+traces_len*3.4/1000*i
                good_spikes = np.hstack(good_spikes)    
                y, bin_edges = np.histogram(good_spikes, np.arange(0, traces_len*3.4/1000*num_trial, bin_width))
                y=y[good_period_idx]    
                
                r2s=np.zeros(len(taus))
                weights_t=np.zeros((len(taus),9))
                predict_ys=np.zeros((len(taus),len(y)))
                for i, tau in enumerate(taus):
                    y_roll=np.roll(y,tau)
                    glm_poiss = sm.GLM(y_roll, sm.add_constant(V_design_matrix), family=sm.families.Poisson(link=sm_log_Link))
                    
                    try:
                        glm_result = glm_poiss.fit()
                        
                        sst_val = sum(map(lambda x: np.power(x,2),y_roll-np.mean(y_roll))) 
                        sse_val = sum(map(lambda x: np.power(x,2),glm_result.resid_response)) 
                        r2 = 1.0 - sse_val/sst_val
                        
                        r2s[i] = r2
                        
                        y_roll_t_p=glm_result.predict(sm.add_constant(V_design_matrix))
                        predict_ys[i,:]=y_roll_t_p
                        weights_t[i,:] = glm_result.params
                        
                    except:
                        pass
                    
                #units_glm.append({**unit_key, 'r2_nolick': r2s, 'weights_nolick': weights_t, 'y_nolick': y, 'predict_y_nolick': predict_ys, 'x_nolick': V_design_matrix})
                print(unit_key)
                ib.insert1({**unit_key, 'r2_nolick': r2s, 'weights_nolick': weights_t, 'y_nolick': y, 'predict_y_nolick': predict_ys, 'x_nolick': V_design_matrix})
                if ib.flush():
                    pass
                        
        #self.insert(units_glm, ignore_extra_fields=True)

@schema
class GLMFitNoLickBody(dj.Computed):
    definition = """
    -> ephys.Unit
    ---
    r2_nolick: mediumblob
    weights_nolick: mediumblob
    y_nolick: longblob
    predict_y_nolick: longblob
    x_nolick: longblob
    """
    # mtl sessions only
    key_source = experiment.Session & v_tracking.TongueTracking3DBot & experiment.Breathing & v_oralfacial_analysis.WhiskerSVD & ephys.Unit & 'rig = "RRig-MTL"'
    
    def make(self, key):
        good_units=ephys.Unit * ephys.ClusterMetric * ephys.UnitStat & key & 'presence_ratio > 0.9' & 'amplitude_cutoff < 0.15' & 'avg_firing_rate > 0.2' & 'isi_violation < 10' & 'unit_amp > 150'
        unit_keys=good_units.fetch('KEY')
        bin_width = 0.017
        num_frame = 1471
        num_frame_b = 500
        
        bad_trial_side,bad_trial_bot,miss_trial_side,miss_trial_bot=(v_oralfacial_analysis.BadVideo & key).fetch('bad_trial_side','bad_trial_bot','miss_trial_side','miss_trial_bot')
        if (bad_trial_side[0] is None):
            bad_trial_side[0]=np.array([0])
        if (miss_trial_side[0] is None):
            miss_trial_side[0]=np.array([0])
        if (bad_trial_bot[0] is None):
            bad_trial_bot[0]=np.array([0])
        if (miss_trial_bot[0] is None):
            miss_trial_bot[0]=np.array([0])    
        bad_trials=np.concatenate((bad_trial_side[0],bad_trial_bot[0],miss_trial_side[0],miss_trial_bot[0]))

        # from the cameras
        tongue_thr = 0.95
        traces_s = tracking.Tracking.TongueTracking - [{'trial': tr} for tr in bad_trials] & key & {'tracking_device': 'Camera 3'} 
        traces_b = tracking.Tracking.TongueTracking - [{'trial': tr} for tr in bad_trials] & key & {'tracking_device': 'Camera 4'}
        
        if len(ephys.Unit.TrialSpikes - [{'trial': tr} for tr in bad_trials] & unit_keys[0]) != len(traces_s):
            print(f'Mismatch in tracking trial and ephys trial number: {key}')
            return
        if len(ephys.Unit.TrialSpikes - [{'trial': tr} for tr in bad_trials] & unit_keys[0]) != len(traces_b):
            print(f'Mismatch in tracking trial and ephys trial number: {key}')
            return
        
        # from the cameras
        tongue_thr = 0.95
        trial_key=(v_tracking.TongueTracking3DBot - [{'trial': tr} for tr in bad_trials] & key).fetch('trial', order_by='trial')
        traces_s = tracking.Tracking.TongueTracking - [{'trial': tr} for tr in bad_trials] & key & {'tracking_device': 'Camera 3'} & [{'trial': tr} for tr in trial_key]
        traces_b = tracking.Tracking.TongueTracking - [{'trial': tr} for tr in bad_trials] & key & {'tracking_device': 'Camera 4'} & [{'trial': tr} for tr in trial_key]
        session_traces_s_l = traces_s.fetch('tongue_likelihood', order_by='trial')
        session_traces_b_l = traces_b.fetch('tongue_likelihood', order_by='trial')

        session_traces_s_l = np.vstack(session_traces_s_l)
        session_traces_b_l = np.vstack(session_traces_b_l)
        session_traces_t_l = session_traces_b_l
        session_traces_t_l[np.where((session_traces_s_l > tongue_thr) & (session_traces_b_l > tongue_thr))] = 1
        session_traces_t_l[np.where((session_traces_s_l <= tongue_thr) | (session_traces_b_l <= tongue_thr))] = 0
        session_traces_t_l = np.hstack(session_traces_t_l)

        session_traces_s_l_f = np.vstack(session_traces_s_l)
        session_traces_b_l_f = np.vstack(session_traces_b_l)
        session_traces_t_l_f = session_traces_b_l_f
        session_traces_t_l_f[np.where((session_traces_s_l_f > tongue_thr) & (session_traces_b_l_f > tongue_thr))] = 1
        session_traces_t_l_f[np.where((session_traces_s_l_f <= tongue_thr) | (session_traces_b_l_f <= tongue_thr))] = 0

        # from 3D calibration
        traces_s = v_tracking.JawTracking3DSid & key & [{'trial': tr} for tr in trial_key]
        traces_b = v_tracking.TongueTracking3DBot & key & [{'trial': tr} for tr in trial_key]
        session_traces_s_y, session_traces_s_x, session_traces_s_z = traces_s.fetch('jaw_y', 'jaw_x', 'jaw_z', order_by='trial')
        session_traces_b_y, session_traces_b_x, session_traces_b_z = traces_b.fetch('tongue_y', 'tongue_x', 'tongue_z', order_by='trial')
        session_traces_s_y = stats.zscore(np.vstack(session_traces_s_y),axis=None)
        session_traces_s_x = stats.zscore(np.vstack(session_traces_s_x),axis=None)
        session_traces_s_z = stats.zscore(np.vstack(session_traces_s_z),axis=None)
        session_traces_b_y = np.vstack(session_traces_b_y)
        traces_y_mean=np.mean(session_traces_b_y[np.where(session_traces_t_l_f == 1)])
        traces_y_std=np.std(session_traces_b_y[np.where(session_traces_t_l_f == 1)])
        session_traces_b_y = (session_traces_b_y - traces_y_mean)/traces_y_std
        session_traces_b_x = np.vstack(session_traces_b_x)
        traces_x_mean=np.mean(session_traces_b_x[np.where(session_traces_t_l_f == 1)])
        traces_x_std=np.std(session_traces_b_x[np.where(session_traces_t_l_f == 1)])
        session_traces_b_x = (session_traces_b_x - traces_x_mean)/traces_x_std
        session_traces_b_z = np.vstack(session_traces_b_z)
        traces_z_mean=np.mean(session_traces_b_z[np.where(session_traces_t_l_f == 1)])
        traces_z_std=np.std(session_traces_b_z[np.where(session_traces_t_l_f == 1)])
        session_traces_b_z = (session_traces_b_z - traces_z_mean)/traces_z_std

        traces_len = np.size(session_traces_b_z, axis = 1)
        num_trial = np.size(session_traces_b_z, axis = 0)

        # format the video data
        session_traces_s_y = np.hstack(session_traces_s_y)
        session_traces_s_x = np.hstack(session_traces_s_x)
        session_traces_s_z = np.hstack(session_traces_s_z)
        session_traces_b_y = np.hstack(session_traces_b_y)
        session_traces_b_x = np.hstack(session_traces_b_x)
        session_traces_b_z = np.hstack(session_traces_b_z)
        # -- moving-average and down-sample
        window_size = int(bin_width/0.0034)  # sample
        # kernel = np.ones(window_size) / window_size
        # session_traces_s_x = np.convolve(session_traces_s_x, kernel, 'same')
        session_traces_s_x = signal.medfilt(session_traces_s_x, window_size)
        session_traces_s_x = session_traces_s_x[window_size::window_size]
        # session_traces_s_y = np.convolve(session_traces_s_y, kernel, 'same')
        session_traces_s_y = signal.medfilt(session_traces_s_y, window_size)
        session_traces_s_y = session_traces_s_y[window_size::window_size]
        # session_traces_s_z = np.convolve(session_traces_s_z, kernel, 'same')
        session_traces_s_z = signal.medfilt(session_traces_s_z, window_size)
        session_traces_s_z = session_traces_s_z[window_size::window_size]
        # session_traces_b_x = np.convolve(session_traces_b_x, kernel, 'same')
        session_traces_b_x = signal.medfilt(session_traces_b_x, window_size)
        session_traces_b_x = session_traces_b_x[window_size::window_size]
        # session_traces_b_y = np.convolve(session_traces_b_y, kernel, 'same')
        session_traces_b_y = signal.medfilt(session_traces_b_y, window_size)
        session_traces_b_y = session_traces_b_y[window_size::window_size]
        # session_traces_b_z = np.convolve(session_traces_b_z, kernel, 'same')
        session_traces_b_z = signal.medfilt(session_traces_b_z, window_size)
        session_traces_b_z = session_traces_b_z[window_size::window_size]
        # session_traces_t_l = np.convolve(session_traces_t_l, kernel, 'same')
        session_traces_t_l = signal.medfilt(session_traces_t_l, window_size)
        session_traces_t_l = session_traces_t_l[window_size::window_size]
        session_traces_t_l[np.where(session_traces_t_l < 1)] = 0
        session_traces_s_x = np.reshape(session_traces_s_x,(-1,1))
        session_traces_s_y = np.reshape(session_traces_s_y,(-1,1))
        session_traces_s_z = np.reshape(session_traces_s_z,(-1,1))
        session_traces_b_x = np.reshape(session_traces_b_x * session_traces_t_l, (-1,1))
        session_traces_b_y = np.reshape(session_traces_b_y * session_traces_t_l, (-1,1))
        session_traces_b_z = np.reshape(session_traces_b_z * session_traces_t_l, (-1,1))

        # get breathing
        breathing, breathing_ts = (experiment.Breathing - [{'trial': tr} for tr in bad_trials] & key & [{'trial': tr} for tr in trial_key]).fetch('breathing', 'breathing_timestamps', order_by='trial')
        good_breathing = breathing
        for i, d in enumerate(breathing):
            good_breathing[i] = d[breathing_ts[i] < traces_len*3.4/1000]
        good_breathing = stats.zscore(np.vstack(good_breathing),axis=None)

        good_breathing = np.hstack(good_breathing)
        # -- moving-average
        window_size = int(round(bin_width/(breathing_ts[0][1]-breathing_ts[0][0]),0))  # sample
        kernel = np.ones(window_size) / window_size
        good_breathing = np.convolve(good_breathing, kernel, 'same')
        # -- down-sample
        good_breathing = good_breathing[window_size::window_size]
        good_breathing = np.reshape(good_breathing,(-1,1))

        # get whisker
        session_traces_w = (v_oralfacial_analysis.WhiskerSVD & key).fetch('mot_svd')
        if len(session_traces_w[0][:,0]) % num_frame != 0:
            print('Bad videos in bottom view')
            return
        else:
            num_trial_w = int(len(session_traces_w[0][:,0])/num_frame)
            session_traces_w = np.reshape(session_traces_w[0][:,0], (num_trial_w, num_frame))
            
        trial_idx_nat = [d.astype(str) for d in np.arange(num_trial_w)]
        trial_idx_nat = sorted(range(len(trial_idx_nat)), key=lambda k: trial_idx_nat[k])
        trial_idx_nat = sorted(range(len(trial_idx_nat)), key=lambda k: trial_idx_nat[k])
        session_traces_w = session_traces_w[trial_idx_nat,:]
        session_traces_w_o = stats.zscore(session_traces_w,axis=None)
        if (np.median(session_traces_w_o) > (np.mean(session_traces_w_o)+0.1)): # flip the negative svd
            session_traces_w_o=session_traces_w_o*-1
        session_traces_w = session_traces_w_o[trial_key-1]
           
        session_traces_w = np.hstack(session_traces_w)
        window_size = int(bin_width/0.0034)  # sample
        kernel = np.ones(window_size) / window_size
        session_traces_w = np.convolve(session_traces_w, kernel, 'same')
        session_traces_w = session_traces_w[window_size::window_size]
        session_traces_w = np.reshape(session_traces_w,(-1,1))
        
        # get body
        session_traces_b = (v_oralfacial_analysis.BodySVD & key).fetch('mot_svd_body')
        if len(session_traces_b[0][:,0]) % num_frame_b != 0:
            print('Bad videos in bottom view')
            return
        else:
            num_trial_b = int(len(session_traces_b[0][:,0])/num_frame_b)
            session_traces_b = np.reshape(session_traces_b[0][:,0], (num_trial_b, num_frame_b))
            
        trial_idx_nat = [d.astype(str) for d in np.arange(num_trial_b)]
        trial_idx_nat = sorted(range(len(trial_idx_nat)), key=lambda k: trial_idx_nat[k])
        trial_idx_nat = sorted(range(len(trial_idx_nat)), key=lambda k: trial_idx_nat[k])
        session_traces_b = session_traces_b[trial_idx_nat,:]
        session_traces_b_o = stats.zscore(session_traces_b,axis=None)
        if (np.median(session_traces_b_o) > (np.mean(session_traces_b_o)+0.1)): # flip the negative svd
            session_traces_b_o=session_traces_b_o*-1
        session_traces_b = session_traces_b_o[trial_key-1]
           
        session_traces_b = np.hstack(session_traces_b)
        session_traces_b = np.reshape(session_traces_b,(-1,1))
        session_traces_b=signal.resample(session_traces_b,len(session_traces_b_x))

        # stimulus
        lick_onset_time,lick_offset_time=(v_oralfacial_analysis.MovementTiming & key).fetch1('lick_onset','lick_offset')

        all_period_idx=np.arange(len(session_traces_b_y))
        good_period_idx=[all_period_idx[(all_period_idx*bin_width<lick_onset_time[1]-0.2)]] # restrict by whisking bouts
        for i,val in enumerate(lick_onset_time[1:]):
            good_period_idx.append(all_period_idx[(all_period_idx*bin_width<lick_onset_time[i+1]-0.2) & (all_period_idx*bin_width>lick_offset_time[i]+0.2)])
        good_period_idx.append(all_period_idx[(all_period_idx*bin_width>lick_offset_time[-1]+0.2)])
        good_period_idx=np.array(good_period_idx)
        good_period_idx=np.hstack(good_period_idx)

        session_traces_s_x=stats.zscore(session_traces_s_x[good_period_idx])
        session_traces_s_y=stats.zscore(session_traces_s_y[good_period_idx])
        session_traces_s_z=stats.zscore(session_traces_s_z[good_period_idx])
        session_traces_b_x=session_traces_b_x[good_period_idx]
        traces_x_mean=np.mean(session_traces_b_x[session_traces_b_x != 0])
        traces_x_std=np.std(session_traces_b_x[session_traces_b_x != 0])
        session_traces_b_x = (session_traces_b_x - traces_x_mean)/traces_x_std
        session_traces_b_y=session_traces_b_y[good_period_idx]
        traces_y_mean=np.mean(session_traces_b_y[session_traces_b_y != 0])
        traces_y_std=np.std(session_traces_b_y[session_traces_b_y != 0])
        session_traces_b_y = (session_traces_b_y - traces_y_mean)/traces_y_std
        session_traces_b_z=session_traces_b_z[good_period_idx]
        traces_z_mean=np.mean(session_traces_b_z[session_traces_b_z != 0])
        traces_z_std=np.std(session_traces_b_z[session_traces_b_z != 0])
        session_traces_b_z = (session_traces_b_z - traces_z_mean)/traces_z_std
        good_breathing=stats.zscore(good_breathing[good_period_idx])
        session_traces_w=stats.zscore(session_traces_w[good_period_idx])
        session_traces_b=stats.zscore(session_traces_b[good_period_idx])

        V_design_matrix = np.concatenate((session_traces_s_x, session_traces_s_y, session_traces_s_z, session_traces_b_x, session_traces_b_y, session_traces_b_z, good_breathing, session_traces_w, session_traces_b), axis=1)
        
        #set up GLM
        sm_log_Link = sm.genmod.families.links.log

        taus = np.arange(-5,6)

        #units_glm = []
        
        with InsertBuffer(self, 10, skip_duplicates=True, ignore_extra_fields=True, allow_direct_insert=True) as ib:
            
            for unit_key in unit_keys: # loop for each neuron
    
                all_spikes=(ephys.Unit.TrialSpikes - [{'trial': tr} for tr in bad_trials] & unit_key & [{'trial': tr} for tr in trial_key]).fetch('spike_times', order_by='trial')
                
                good_spikes =all_spikes # get good spikes
                for i, d in enumerate(good_spikes):
                    good_spikes[i] = d[d < traces_len*3.4/1000]+traces_len*3.4/1000*i
                good_spikes = np.hstack(good_spikes)    
                y, bin_edges = np.histogram(good_spikes, np.arange(0, traces_len*3.4/1000*num_trial, bin_width))
                y=y[good_period_idx]    
                
                r2s=np.zeros(len(taus))
                weights_t=np.zeros((len(taus),10))
                predict_ys=np.zeros((len(taus),len(y)))
                for i, tau in enumerate(taus):
                    y_roll=np.roll(y,tau)
                    glm_poiss = sm.GLM(y_roll, sm.add_constant(V_design_matrix), family=sm.families.Poisson(link=sm_log_Link))
                    
                    try:
                        glm_result = glm_poiss.fit()
                        
                        sst_val = sum(map(lambda x: np.power(x,2),y_roll-np.mean(y_roll))) 
                        sse_val = sum(map(lambda x: np.power(x,2),glm_result.resid_response)) 
                        r2 = 1.0 - sse_val/sst_val
                        
                        r2s[i] = r2
                        
                        y_roll_t_p=glm_result.predict(sm.add_constant(V_design_matrix))
                        predict_ys[i,:]=y_roll_t_p
                        weights_t[i,:] = glm_result.params
                        
                    except:
                        pass
                    
                #units_glm.append({**unit_key, 'r2_nolick': r2s, 'weights_nolick': weights_t, 'y_nolick': y, 'predict_y_nolick': predict_ys, 'x_nolick': V_design_matrix})
                print(unit_key)
                ib.insert1({**unit_key, 'r2_nolick': r2s, 'weights_nolick': weights_t, 'y_nolick': y, 'predict_y_nolick': predict_ys, 'x_nolick': V_design_matrix})
                if ib.flush():
                    pass
                        
        #self.insert(units_glm, ignore_extra_fields=True)

@schema
class GLMFitCAE(dj.Computed):
    definition = """
    -> ephys.Unit
    ---
    r2_cae: mediumblob
    r2_t_cae: mediumblob
    weights_cae: mediumblob
    predict_y_cae: longblob
    test_y_cae: longblob
    test_x_cae: longblob
    """
    # mtl sessions only
    key_source = experiment.Session & v_tracking.TongueTracking3DBot & experiment.Breathing & v_oralfacial_analysis.CaeEmbeddingOcc & ephys.Unit & 'rig = "RRig-MTL"'
    
    def make(self, key):
        good_units=ephys.Unit * ephys.ClusterMetric * ephys.UnitStat & key & 'presence_ratio > 0.9' & 'amplitude_cutoff < 0.15' & 'avg_firing_rate > 0.2' & 'isi_violation < 10' & 'unit_amp > 150'
        unit_keys=good_units.fetch('KEY')
        traces_len=1471
        traces_len_c=295
        bin_width=traces_len/traces_len_c*3.4/1000
        
        # from the cameras
        traces_s = tracking.Tracking.TongueTracking & key & {'tracking_device': 'Camera 3'} 
        traces_b = tracking.Tracking.TongueTracking & key & {'tracking_device': 'Camera 4'}
        
        if len(experiment.SessionTrial & (ephys.Unit.TrialSpikes & key)) != len(traces_s):
            print(f'Mismatch in tracking trial and ephys trial number: {key}')
            return
        if len(experiment.SessionTrial & (ephys.Unit.TrialSpikes & key)) != len(traces_b):
            print(f'Mismatch in tracking trial and ephys trial number: {key}')
            return
        
        # from the cameras
        trial_key_o=(v_tracking.TongueTracking3DBot & key).fetch('trial', order_by='trial')
        test_t = trial_key_o[::5] # test trials
        trial_key=np.setdiff1d(trial_key_o,test_t)
        num_trial_t=len(test_t)
        num_trial=len(trial_key)
        
        embedding_side=(v_oralfacial_analysis.CaeEmbeddingOcc.EmbeddingPart & 'part_name="side"' & [{'trial': tr} for tr in trial_key] & key).fetch('embedding_occ', order_by='trial')
        V_design_matrix=np.vstack(embedding_side)
        
        embedding_side_t=(v_oralfacial_analysis.CaeEmbeddingOcc.EmbeddingPart & 'part_name="side"' & [{'trial': tr} for tr in test_t] & key).fetch('embedding_occ', order_by='trial')
        V_design_matrix_t=np.vstack(embedding_side_t)
        
        #set up GLM
        sm_log_Link = sm.genmod.families.links.log

        taus = np.arange(-5,6)

        #units_glm = []
        
        with InsertBuffer(self, 10, skip_duplicates=True, ignore_extra_fields=True, allow_direct_insert=True) as ib:
            
            for unit_key in unit_keys: # loop for each neuron
    
                all_spikes=(ephys.Unit.TrialSpikes & unit_key & [{'trial': tr} for tr in trial_key]).fetch('spike_times', order_by='trial')                
                good_spikes =all_spikes # get good spikes
                for i, d in enumerate(good_spikes):
                    good_spikes[i] = d[d < traces_len*3.4/1000]+traces_len*3.4/1000*i
                good_spikes = np.hstack(good_spikes)    
                y, bin_edges = np.histogram(good_spikes, np.arange(0, (traces_len_c*num_trial+0.5)*bin_width, bin_width))
                
                all_spikes=(ephys.Unit.TrialSpikes & unit_key & [{'trial': tr} for tr in test_t]).fetch('spike_times', order_by='trial')                
                good_spikes=all_spikes # get good spikes
                for i, d in enumerate(good_spikes):
                    good_spikes[i] = d[d < traces_len*3.4/1000]+traces_len*3.4/1000*i
                good_spikes = np.hstack(good_spikes)    
                y_t, bin_edges = np.histogram(good_spikes, np.arange(0, (traces_len_c*num_trial_t+0.5)*bin_width, bin_width))
                                   
                r2s=np.zeros(len(taus))
                r2s_t=r2s
                weights_t=np.zeros((len(taus),V_design_matrix.shape[1]+1))
                predict_ys=np.zeros((len(taus),len(y_t)))
                for i, tau in enumerate(taus):
                    y_roll=np.roll(y,tau)
                    y_roll_t=np.roll(y_t,tau)
                    glm_poiss = sm.GLM(y_roll, sm.add_constant(V_design_matrix), family=sm.families.Poisson(link=sm_log_Link))
                    
                    try:
                        glm_result = glm_poiss.fit()
                        
                        sst_val = sum(map(lambda x: np.power(x,2),y_roll-np.mean(y_roll))) 
                        sse_val = sum(map(lambda x: np.power(x,2),glm_result.resid_response)) 
                        r2 = 1.0 - sse_val/sst_val
                        
                        r2s[i] = r2
                        
                        y_roll_t_p=glm_result.predict(sm.add_constant(V_design_matrix_t))
                        sst_val = sum(map(lambda x: np.power(x,2),y_roll_t-np.mean(y_roll_t))) 
                        sse_val = sum(map(lambda x: np.power(x,2),y_roll_t-y_roll_t_p)) 
                        r2s_t[i] = 1.0 - sse_val/sst_val
                        predict_ys[i,:]=y_roll_t_p
                        weights_t[i,:] = glm_result.params
                                                
                    except:
                        pass
                    
                #units_glm.append({**unit_key, 'r2_nolick': r2s, 'weights_nolick': weights_t, 'y_nolick': y, 'predict_y_nolick': predict_ys, 'x_nolick': V_design_matrix})
                print(unit_key)
                ib.insert1({**unit_key, 'r2_cae': r2s, 'r2_t_cae': r2s_t, 'weights_cae': weights_t, 'test_y_cae': y_t, 'predict_y_cae': predict_ys, 'test_x_cae': V_design_matrix_t})
                if ib.flush():
                    pass

@schema
class WhiskerSVD(dj.Computed):
    definition = """
    -> experiment.Session
    ---
    mot_svd: longblob
    """
    
    key_source = experiment.Session & 'rig = "RRig-MTL"' & (tracking.Tracking  & 'tracking_device = "Camera 4"')
    
    def make(self, key):
        
        from facemap import process
        
        roi_path = 'H://videos//bottom//DL027//2021_07_01//DL027_2021_07_01_bottom_0_proc.npy'
        roi_data = np.load(roi_path, allow_pickle=True).item()
        
        video_root_dir = pathlib.Path('H:/videos')
        #video_root_dir = pathlib.Path('I:/videos')
        
        trials=(tracking_ingest.TrackingIngest.TrackingFile & 'tracking_device = "Camera 4"' & key).fetch('trial')
        
        trial_path = (tracking_ingest.TrackingIngest.TrackingFile & 'tracking_device = "Camera 4"' & {'trial': trials[-1]} & key).fetch1('tracking_file')
        
        video_path = video_root_dir / trial_path
        
        video_path = video_path.parent
        
        video_files = list(video_path.glob('*.mp4'))
        video_files_l = [[str(video_files[0])]]
        for ind_trial, file in enumerate(video_files[1:]):
            video_files_l.append([str(file)])
            
        proc = process.run(video_files_l, proc=roi_data)
        
        self.insert1({**key, 'mot_svd': proc['motSVD'][1][:, :3]})

@schema
class BodySVD(dj.Computed):
    definition = """
    -> experiment.Session
    ---
    mot_svd_body: longblob
    """
    
    key_source = experiment.Session & 'rig = "RRig-MTL"' & (tracking.Tracking  & 'tracking_device = "Camera 4"')
    
    def make(self, key):
        
        from facemap import process
        
        if key['subject_id']==2897:
            roi_path = 'H://videos//body//DL004//2021_03_08//DL004_2021_03_08_body_0_proc.npy'
        else:
            roi_path = 'H://videos//body//DL027//2021_07_01//DL027_2021_07_01_body_0_proc.npy'
        
        roi_data = np.load(roi_path, allow_pickle=True).item()
        
        # video_root_dir = pathlib.Path('H:/videos')
        video_root_dir = pathlib.Path('I:/videos')
        
        trials=(tracking_ingest.TrackingIngest.TrackingFile & 'tracking_device = "Camera 4"' & key).fetch('trial')
        
        trial_path = (tracking_ingest.TrackingIngest.TrackingFile & 'tracking_device = "Camera 4"' & {'trial': trials[-1]} & key).fetch1('tracking_file')
        
        video_path = video_root_dir / trial_path
        
        video_path = video_path.parent
        
        video_files = list(video_path.glob('*.mp4'))
        video_files_l = [[str(video_files[0]).replace('bottom','body')]]
        for ind_trial, file in enumerate(video_files[1:]):
            video_files_l.append([str(file).replace('bottom','body')])
            
        proc = process.run(video_files_l, proc=roi_data)
        
        self.insert1({**key, 'mot_svd_body': proc['motSVD'][1][:, :3]})
        
@schema
class BottomSVD(dj.Computed):
    definition = """
    -> experiment.Session
    ---
    mot_svd_bot: longblob
    """
    
    key_source = experiment.Session & 'rig = "RRig-MTL"' & (tracking.Tracking  & 'tracking_device = "Camera 4"')
    
    def make(self, key):
        
        from facemap import process
        
        #roi_path = 'H://videos//bottom//DL027//2021_07_01//DL027_2021_07_01_bottom_0_proc.npy'
        roi_path = 'H://videos//bottom//DL017//2021_07_14//DL017_2021_07_14_bottom_0_proc.npy'
        roi_data = np.load(roi_path, allow_pickle=True).item()
        
        video_root_dir = pathlib.Path('H:/videos')
        # video_root_dir = pathlib.Path('I:/videos')
        
        trial_path = (tracking_ingest.TrackingIngest.TrackingFile & 'tracking_device = "Camera 4"' & 'trial = 1' & key).fetch1('tracking_file')
        
        video_path = video_root_dir / trial_path
        
        video_path = video_path.parent
        
        video_files = list(video_path.glob('*.mp4'))
        video_files_l = [[str(video_files[0])]]
        for ind_trial, file in enumerate(video_files[1:]):
            video_files_l.append([str(file)])
            
        proc = process.run(video_files_l, proc=roi_data)
        
        self.insert1({**key, 'mot_svd_bot': proc['motSVD'][1][:, :16]})
        
@schema
class SideSVD(dj.Computed):
    definition = """
    -> experiment.Session
    ---
    mot_svd_side: longblob
    svd_mask_side: longblob
    """
    
    key_source = experiment.Session & 'rig = "RRig-MTL"' & (tracking.Tracking  & 'tracking_device = "Camera 3"')
    
    def make(self, key):
        
        from facemap import process
        
        roi_path = 'H://videos//side//DL027//2021_07_01//DL027_2021_07_01_side_0_proc.npy'
        roi_data = np.load(roi_path, allow_pickle=True).item()
        
        video_root_dir = pathlib.Path('H:/videos')
        
        trial_path = (tracking_ingest.TrackingIngest.TrackingFile & 'tracking_device = "Camera 3"' & 'trial = 1' & key).fetch1('tracking_file')
        
        video_path = video_root_dir / trial_path
        
        video_path = video_path.parent
        
        video_files = list(video_path.glob('*.mp4'))
        video_files_l = [[str(video_files[0])]]
        for ind_trial, file in enumerate(video_files[1:]):
            video_files_l.append([str(file)])
            
        proc = process.run(video_files_l, proc=roi_data)
        
        self.insert1({**key, 'mot_svd_side': proc['motSVD'][1][:, :16], 'svd_mask_side': proc['motMask_reshape'][1][:,:,:16]})

@schema
class ContactLick(dj.Computed):
    definition = """
    -> tracking.Tracking
    ---
    contact_times: mediumblob
    """
    
    key_source = experiment.Session & v_tracking.TongueTracking3DBot & v_tracking.LickPortTracking3DBot & ephys.Unit & 'rig = "RRig-MTL"'
    
    def make(self, key):
        ts = 0.0034
        radius=1
        ton_thr = 0.95
        
        bot_ton_x, bot_ton_y, bot_ton_z,trials = (v_tracking.TongueTracking3DBot & key).fetch('tongue_x','tongue_y','tongue_z','trial',order_by = 'trial')
        bot_tongue_l = (v_tracking.Tracking.TongueTracking & key & 'tracking_device = "Camera 4"' & [{'trial': tr} for tr in trials]).fetch('tongue_likelihood', order_by = 'trial')
        sid_tongue_l = (v_tracking.Tracking.TongueTracking & key & 'tracking_device = "Camera 3"' & [{'trial': tr} for tr in trials]).fetch('tongue_likelihood', order_by = 'trial')
        bot_lic_x, bot_lic_y, bot_lic_z = (v_tracking.LickPortTracking3DBot & key).fetch('lickport_x','lickport_y','lickport_z', order_by = 'trial')

        bot_tongue_l = np.vstack(bot_tongue_l)
        sid_tongue_l = np.vstack(sid_tongue_l)
        likelihood = bot_tongue_l
        likelihood[np.where((sid_tongue_l > ton_thr) & (bot_tongue_l > ton_thr))] = 1
        likelihood[np.where((sid_tongue_l <= ton_thr) | (bot_tongue_l <= ton_thr))] = 0

        bot_ton_x=np.vstack(bot_ton_x)
        bot_ton_y=np.vstack(bot_ton_y)
        bot_ton_z=np.vstack(bot_ton_z)
        bot_lic_x=np.vstack(bot_lic_x)
        bot_lic_y=np.vstack(bot_lic_y)
        bot_lic_z=np.vstack(bot_lic_z)

        trial_contact = []

        for i in np.arange(np.size(bot_ton_x,axis=0)):
            lickSpan=np.where(likelihood[i,:]==1)[0]
            lickBreak=np.diff(lickSpan)
            lickS=np.concatenate(([0], np.where(lickBreak>1)[0]+1))
            
            contacts = []
            if len(lickS)>1:
                lickS1=lickSpan[lickS]
                lickE1=np.concatenate((lickSpan[lickS[1:]-1], [lickSpan[-1]]))
                lick_x_med=np.median(bot_lic_x[i,350:])
                lick_y_med=np.median(bot_lic_y[i,350:])
                lick_z_med=np.median(bot_lic_z[i,350:])       
                
                for j in np.arange(len(lickS1)):
                    xp=bot_ton_x[i,lickS1[j]:lickE1[j]]
                    yp=bot_ton_y[i,lickS1[j]:lickE1[j]]
                    zp=bot_ton_z[i,lickS1[j]:lickE1[j]]
                    inside=np.where(((xp-lick_x_med)**2 + (yp-lick_y_med)**2 + (zp-lick_z_med)**2) < radius**2)
                    if lickE1[j]-lickS1[j]>10 and lickE1[j]-lickS1[j]<35  and np.size(inside)>0:
                        contacts.append(lickS1[j]*ts)              
            trial_contact.append({**key, 'trial': trials[i], 'tracking_device': 'Camera 4', 'contact_times': contacts})
            
        self.insert(trial_contact, ignore_extra_fields=True)

@schema
class DirectionTuning(dj.Computed):
    definition = """
    -> ephys.Unit
    ---
    direction_tuning: mediumblob
    direction_index: float
    preferred_phase: float
    
    """

    key_source = experiment.Session & v_oralfacial_analysis.ContactLick & ephys.Unit & 'rig = "RRig-MTL"'
    
    def make(self, key):
        good_units=ephys.Unit * ephys.ClusterMetric * ephys.UnitStat & key & 'presence_ratio > 0.9' & 'amplitude_cutoff < 0.15' & 'avg_firing_rate > 0.2' & 'isi_violation < 10' & 'unit_amp > 150'
        unit_keys=good_units.fetch('KEY')

        contact_times, trials,water_port=(v_oralfacial_analysis.ContactLick * experiment.MultiTargetLickingSessionBlock.BlockTrial * experiment.MultiTargetLickingSessionBlock.WaterPort & key).fetch('contact_times','trial','water_port', order_by = 'trial')

        unit_dir=[]
        for unit_key in unit_keys: # loop for each neuron
            all_spikes=(ephys.Unit.TrialSpikes & unit_key & [{'trial': tr} for tr in trials]).fetch('spike_times', order_by='trial')
            direction_spk=np.zeros(9)
            direction_lick=np.zeros(9)
            for i in np.arange(len(trials)):
                tr_fr=np.zeros(len(contact_times[i]))
                dir_idx=int(water_port[i][-1])-1
                for j in np.arange(len(tr_fr)):
                    tr_fr[j], _ = np.histogram(all_spikes[i], bins=1, range=(contact_times[i][j]-.05, contact_times[i][j]+.1))
                direction_spk[dir_idx]=direction_spk[dir_idx]+sum(tr_fr)
                direction_lick[dir_idx]=direction_lick[dir_idx]+len(tr_fr)
                
            direction_tun=direction_spk/direction_lick
            
            tuning_y=direction_tun[[7,8,5,2,1,0,3,6]]
            tuning_x=np.linspace(0,7*np.pi/4,8)
            tuning_y_n=tuning_y[~np.isnan(tuning_y)]
            tuning_x_n=tuning_x[~np.isnan(tuning_y)]
            pref_phase,dir_idx=helper_functions.compute_phase_tuning(tuning_x_n, tuning_y_n)
            if np.isnan(dir_idx):
                dir_idx=0
                pref_phase=0
            unit_dir.append({**unit_key, 'direction_tuning': direction_tun, 'preferred_phase': pref_phase, 'direction_index': dir_idx})
            
        self.insert(unit_dir, ignore_extra_fields=True)
        
@schema
class LickLatency(dj.Computed):
    definition = """
    -> ephys.Unit
    ---    
    latency: float
    
    """

    key_source = experiment.Session & v_oralfacial_analysis.MovementTiming & ephys.Unit & 'rig = "RRig-MTL"'
    
    def make(self, key):
        traces_len=1471

        good_units=ephys.Unit & key & (ephys.UnitPassingCriteria  & 'criteria_passed=1')

        unit_keys=good_units.fetch('KEY')

        trial_key=(v_tracking.TongueTracking3DBot & key).fetch('trial', order_by='trial')
        lick_onset=(v_oralfacial_analysis.MovementTiming & key).fetch('lick_onset')

        t_before=1
        t_after=0.5
        bin_width=0.001
        
        units_lat = []
        
        for unit_key in unit_keys: # loop for each neuron
            all_spikes=(ephys.Unit.TrialSpikes & unit_key & [{'trial': tr} for tr in trial_key]).fetch('spike_times', order_by='trial')   
            good_spikes=all_spikes # get good spikes
            for i, d in enumerate(good_spikes):
                good_spikes[i] = d[d < traces_len*3.4/1000]+traces_len*3.4/1000*i
            good_spikes = np.hstack(good_spikes)    
            
            psth=[]
            
            for lick_t in lick_onset[0]:
                psth.append(good_spikes[(good_spikes>lick_t-t_before) & (good_spikes<lick_t+t_after)]-lick_t)
            
            psth=np.hstack(psth)
            y, bin_edges = np.histogram(psth, np.arange(-t_before, t_after, bin_width))
            
            fr_mean=np.mean(y[0:int((t_before/bin_width)-1)])
            
            thr_cross=np.where(y[int((t_before/bin_width)):]>stats.poisson.ppf(0.95,fr_mean))[0]
            
            latency=0
            for i, thr in enumerate(thr_cross[:-1]):
                if thr_cross[i+1]==thr+1:
                    latency=thr*bin_width
                    break
            units_lat.append({**unit_key, 'latency': latency})
            
        self.insert(units_lat, ignore_extra_fields=True)
        
@schema
class MovementTiming(dj.Computed):
    definition = """
    -> experiment.Session
    ---
    inspiration_onset: mediumblob
    tongue_onset: mediumblob
    lick_onset: mediumblob
    lick_offset: mediumblob
    whisker_onset: mediumblob
    whisk_onset: mediumblob
    whisk_offset: mediumblob
    """
    
    key_source = experiment.Session & v_tracking.TongueTracking3DBot & experiment.Breathing & v_oralfacial_analysis.WhiskerSVD & ephys.Unit & 'rig = "RRig-MTL"'
    
    def make(self, key):
        
        bin_width = 0.0034

        # from the cameras
        tongue_thr = 0.95
        trial_key=(v_tracking.TongueTracking3DBot & key).fetch('trial', order_by='trial')
        traces_s = tracking.Tracking.TongueTracking & key & {'tracking_device': 'Camera 3'} & [{'trial': tr} for tr in trial_key]
        traces_b = tracking.Tracking.TongueTracking & key & {'tracking_device': 'Camera 4'} & [{'trial': tr} for tr in trial_key]
        session_traces_s_l = traces_s.fetch('tongue_likelihood', order_by='trial')
        session_traces_b_l = traces_b.fetch('tongue_likelihood', order_by='trial')

        session_traces_s_l = np.vstack(session_traces_s_l)
        session_traces_b_l = np.vstack(session_traces_b_l)
        session_traces_t_l = session_traces_b_l
        session_traces_t_l[np.where((session_traces_s_l > tongue_thr) & (session_traces_b_l > tongue_thr))] = 1
        session_traces_t_l[np.where((session_traces_s_l <= tongue_thr) | (session_traces_b_l <= tongue_thr))] = 0
        session_traces_t_l = np.hstack(session_traces_t_l)

        session_traces_s_l_f = np.vstack(session_traces_s_l)
        session_traces_b_l_f = np.vstack(session_traces_b_l)
        session_traces_t_l_f = session_traces_b_l_f
        session_traces_t_l_f[np.where((session_traces_s_l_f > tongue_thr) & (session_traces_b_l_f > tongue_thr))] = 1
        session_traces_t_l_f[np.where((session_traces_s_l_f <= tongue_thr) | (session_traces_b_l_f <= tongue_thr))] = 0

        # from 3D calibration
        traces_s = v_tracking.JawTracking3DSid & key & [{'trial': tr} for tr in trial_key]
        traces_b = v_tracking.TongueTracking3DBot & key & [{'trial': tr} for tr in trial_key]
        session_traces_s_y, session_traces_s_x, session_traces_s_z = traces_s.fetch('jaw_y', 'jaw_x', 'jaw_z', order_by='trial')
        session_traces_b_y, session_traces_b_x, session_traces_b_z = traces_b.fetch('tongue_y', 'tongue_x', 'tongue_z', order_by='trial')
        session_traces_s_y = stats.zscore(np.vstack(session_traces_s_y),axis=None)
        session_traces_s_x = stats.zscore(np.vstack(session_traces_s_x),axis=None)
        session_traces_s_z = stats.zscore(np.vstack(session_traces_s_z),axis=None)
        session_traces_b_y = np.vstack(session_traces_b_y)
        traces_y_mean=np.mean(session_traces_b_y[np.where(session_traces_t_l_f == 1)])
        traces_y_std=np.std(session_traces_b_y[np.where(session_traces_t_l_f == 1)])
        session_traces_b_y = (session_traces_b_y - traces_y_mean)/traces_y_std
        session_traces_b_x = np.vstack(session_traces_b_x)
        traces_x_mean=np.mean(session_traces_b_x[np.where(session_traces_t_l_f == 1)])
        traces_x_std=np.std(session_traces_b_x[np.where(session_traces_t_l_f == 1)])
        session_traces_b_x = (session_traces_b_x - traces_x_mean)/traces_x_std
        session_traces_b_z = np.vstack(session_traces_b_z)
        traces_z_mean=np.mean(session_traces_b_z[np.where(session_traces_t_l_f == 1)])
        traces_z_std=np.std(session_traces_b_z[np.where(session_traces_t_l_f == 1)])
        session_traces_b_z = (session_traces_b_z - traces_z_mean)/traces_z_std

        traces_len = np.size(session_traces_b_z, axis = 1)

        # format the video data
        session_traces_s_y = np.hstack(session_traces_s_y)
        session_traces_s_x = np.hstack(session_traces_s_x)
        session_traces_s_z = np.hstack(session_traces_s_z)
        session_traces_b_y = np.hstack(session_traces_b_y)
        session_traces_b_x = np.hstack(session_traces_b_x)
        session_traces_b_z = np.hstack(session_traces_b_z)
        # -- moving-average and down-sample
        window_size = int(bin_width/0.0034)  # sample
        kernel = np.ones(window_size) / window_size
        session_traces_s_x = np.convolve(session_traces_s_x, kernel, 'same')
        session_traces_s_x = session_traces_s_x[window_size::window_size]
        session_traces_s_y = np.convolve(session_traces_s_y, kernel, 'same')
        session_traces_s_y = session_traces_s_y[window_size::window_size]
        session_traces_s_z = np.convolve(session_traces_s_z, kernel, 'same')
        session_traces_s_z = session_traces_s_z[window_size::window_size]
        session_traces_b_x = np.convolve(session_traces_b_x, kernel, 'same')
        session_traces_b_x = session_traces_b_x[window_size::window_size]
        session_traces_b_y = np.convolve(session_traces_b_y, kernel, 'same')
        session_traces_b_y = session_traces_b_y[window_size::window_size]
        session_traces_b_z = np.convolve(session_traces_b_z, kernel, 'same')
        session_traces_b_z = session_traces_b_z[window_size::window_size]
        session_traces_t_l = np.convolve(session_traces_t_l, kernel, 'same')
        session_traces_t_l = session_traces_t_l[window_size::window_size]
        session_traces_t_l[np.where(session_traces_t_l < 1)] = 0
        session_traces_s_x = np.reshape(session_traces_s_x,(-1,1))
        session_traces_s_y = np.reshape(session_traces_s_y,(-1,1))
        session_traces_s_z = np.reshape(session_traces_s_z,(-1,1))
        session_traces_b_x = np.reshape(session_traces_b_x * session_traces_t_l, (-1,1))
        session_traces_b_y = np.reshape(session_traces_b_y * session_traces_t_l, (-1,1))
        session_traces_b_z = np.reshape(session_traces_b_z * session_traces_t_l, (-1,1))

        # get breathing
        breathing, breathing_ts = (experiment.Breathing & key & [{'trial': tr} for tr in trial_key]).fetch('breathing', 'breathing_timestamps', order_by='trial')
        good_breathing = breathing
        for i, d in enumerate(breathing):
            good_breathing[i] = d[breathing_ts[i] < traces_len*3.4/1000]
        good_breathing = stats.zscore(np.vstack(good_breathing),axis=None)

        good_breathing = np.hstack(good_breathing)
        # -- moving-average
        window_size = int(round(bin_width/(breathing_ts[0][1]-breathing_ts[0][0]),0))  # sample
        kernel = np.ones(window_size) / window_size
        good_breathing = np.convolve(good_breathing, kernel, 'same')
        # -- down-sample
        good_breathing = good_breathing[window_size::window_size]
        good_breathing = np.reshape(good_breathing,(-1,1))

        # get whisker
        session_traces_w = (v_oralfacial_analysis.WhiskerSVD & key).fetch('mot_svd')
        if len(session_traces_w[0][:,0]) % 1471 != 0:
            print('Bad videos in bottom view')
            #return
        else:
            num_trial_w = int(len(session_traces_w[0][:,0])/1471)
            session_traces_w = np.reshape(session_traces_w[0][:,0], (num_trial_w, 1471))
            
        trial_idx_nat = [d.astype(str) for d in np.arange(num_trial_w)]
        trial_idx_nat = sorted(range(len(trial_idx_nat)), key=lambda k: trial_idx_nat[k])
        trial_idx_nat = sorted(range(len(trial_idx_nat)), key=lambda k: trial_idx_nat[k])
        session_traces_w = session_traces_w[trial_idx_nat,:]
        session_traces_w_o = stats.zscore(session_traces_w,axis=None)
        session_traces_w = session_traces_w_o[trial_key-1]
           
        session_traces_w = np.hstack(session_traces_w)
        window_size = int(bin_width/0.0034)  # sample
        kernel = np.ones(window_size) / window_size
        session_traces_w = np.convolve(session_traces_w, kernel, 'same')
        session_traces_w = session_traces_w[window_size::window_size]
        session_traces_w = np.reshape(session_traces_w,(-1,1))
        
        # coordination of movements
        amp_b, phase_b=behavior_plot.compute_insta_phase_amp(good_breathing, 1/bin_width, freq_band=(1, 15)) # breathing
        phase_b = phase_b + np.pi

        threshold = np.pi
        cond = (phase_b < threshold) & (np.roll(phase_b,-1) >= threshold)
        inspir_onset=np.argwhere(cond)[:,0]*bin_width # get onset of breath
        a_threshold = -0.5 # amplitude threshold
        a_cond = (good_breathing > a_threshold) & (np.roll(good_breathing,-1) <= a_threshold)
        inspir_amp=np.argwhere(a_cond)[:,0]*bin_width # amp threshold
        inspir_onset_a = [] # only take inspir with a amp crossing
        for i, inspir_value in enumerate(inspir_onset[:-1]):
            if any((inspir_amp>inspir_value) & (inspir_amp<inspir_onset[i+1])):
                inspir_onset_a.append(inspir_value)
        inspir_onset=np.array(inspir_onset_a)

        # licking epochs
        threshold = 0.5 # tongue detection
        a_cond = (session_traces_t_l < threshold) & (np.roll(session_traces_t_l,-1) >= threshold)
        ton_onset=np.argwhere(a_cond)[:,0]*bin_width # get onset of breath

        ilf=1/np.diff(ton_onset)

        ton_onset=ton_onset[:-1] 
        f_cond=(ilf>3) & (ilf<9) # lick freq > 3 & < 9
        ton_onset_idx=np.argwhere(f_cond)[:,0] # index of tongue appearance
        lick_onset_idx=[]
        next_lick=np.diff(ton_onset_idx)
        for i,tongue in enumerate(ton_onset_idx[:-2]):
            #if (next_lick[i]==1) & (next_lick[i+1]==1): # num licks > 3
            if (next_lick[i]==1): # num licks > 3
                lick_onset_idx.append(tongue) # index of tongue
        lick_onset_idx=np.array(lick_onset_idx)
        lick_onset_d=np.diff(lick_onset_idx)
        lick_cond_on = np.roll(lick_onset_d,1) >= 2
        lick_cond_off = lick_onset_d >= 2
        lick_bout_onset=np.argwhere(lick_cond_on)[:,0]
        lick_bout_offset=np.argwhere(lick_cond_off)[:,0]
        if lick_bout_onset[0]!=0:
            lick_bout_onset=np.concatenate((np.array([0]),lick_bout_onset)) # add first lick
            lick_bout_offset=np.concatenate((lick_bout_offset,np.array([len(lick_onset_idx)-1]))) # add last lick

        lick_onset_time=ton_onset[lick_onset_idx[lick_bout_onset]] # get onset of licks
        lick_offset_time=ton_onset[lick_onset_idx[lick_bout_offset]+2]

        # whisking epochs
        if (np.median(session_traces_w) > (np.mean(session_traces_w)+0.1)): # flip the negative svd
            session_traces_w=session_traces_w*-1
        amp_w, phase_w=behavior_plot.compute_insta_phase_amp(session_traces_w, 1/bin_width, freq_band=(3, 25))
        phase_w = phase_w + np.pi

        threshold = 1 # whisking detection
        a_cond = (amp_w < threshold) & (np.roll(amp_w,-1) >= threshold)
        whi_onset=np.argwhere(a_cond)[:,0]*bin_width # get onset of breath

        iwf=1/np.diff(whi_onset)

        whi_onset=whi_onset[:-1] 
        f_cond=(iwf>1) & (iwf<25) # whisk freq > 1 & < 25
        whisker_onset_idx=np.argwhere(f_cond)[:,0] # index of tongue appearance
        whi_onset_idx=[]
        next_whi=np.diff(whisker_onset_idx)
        for i,whisker in enumerate(whisker_onset_idx[:-2]):
            if (next_whi[i]==1) & (next_whi[i+1]==1): # num licks > 3
            #if (next_lick[i]==1): # num licks > 3
                whi_onset_idx.append(whisker) # index of tongue
        whi_onset_idx=np.array(whi_onset_idx)
        whi_onset_d=np.diff(whi_onset_idx)
        whi_cond_on = np.roll(whi_onset_d,1) >= 2
        whi_cond_off = whi_onset_d >= 2
        whi_bout_onset=np.argwhere(whi_cond_on)[:,0]
        whi_bout_offset=np.argwhere(whi_cond_off)[:,0]
        if whi_bout_onset[0]!=0:
            whi_bout_onset=np.concatenate((np.array([0]),whi_bout_onset)) # add first lick
            whi_bout_offset=np.concatenate((whi_bout_offset,np.array([len(whi_onset_idx)-1]))) # add last lick

        whi_onset_time=whi_onset[whi_onset_idx[whi_bout_onset]] # get onset of licks
        whi_offset_time=whi_onset[whi_onset_idx[whi_bout_offset]+2]
        
        self.insert1({**key, 'inspiration_onset': inspir_onset, 'lick_onset': lick_onset_time, 'lick_offset': lick_offset_time, 'whisk_onset': whi_onset_time, 'whisk_offset': whi_offset_time, 'tongue_onset': ton_onset, 'whisker_onset': whi_onset})

@schema
class BadVideo(dj.Computed):
    definition = """
    -> experiment.Session
    ---
    bad_trial_side=null: mediumblob
    bad_trial_bot=null: mediumblob
    miss_trial_side=null: mediumblob
    miss_trial_bot=null: mediumblob
    
    """
    
    key_source = experiment.Session & ephys.Unit & tracking.Tracking & 'rig="RRig-MTL"'
    
    def make(self, key):  
        jaw_y_sid,side_cam_trials=(tracking.Tracking.JawTracking & key & 'tracking_device="Camera 3"').fetch('jaw_y','trial',order_by='trial') #side
        jaw_y_bot,bot_cam_trials=(tracking.Tracking.JawTracking & key & 'tracking_device="Camera 4"').fetch('jaw_y','trial',order_by='trial') #bot
        exp_trials=(experiment.SessionTrial & key).fetch('trial') #exp
        jaw_sid_len = [len(d) for d in jaw_y_sid]
        bad_sid_ind = np.where(np.array(jaw_sid_len) != 1471)[0]

        jaw_bot_len = [len(d) for d in jaw_y_bot]
        bad_bot_ind = np.where(np.array(jaw_bot_len) != 1471)[0]
        miss_tr_sid=None
        if len(side_cam_trials) != len(exp_trials):
            miss_tr_sid=np.setdiff1d(exp_trials,side_cam_trials)
        miss_tr_bot=None    
        if len(bot_cam_trials) != len(exp_trials):
            miss_tr_bot=np.setdiff1d(exp_trials,bot_cam_trials)
        
        self.insert1({**key, 'bad_trial_side': side_cam_trials[bad_sid_ind], 'bad_trial_bot': bot_cam_trials[bad_bot_ind], 'miss_trial_side': miss_tr_sid, 'miss_trial_bot': miss_tr_bot})
        
@schema
class CAEEmbedding(dj.Computed):
    definition = """
    -> experiment.SessionTrial
    ---
    embedding_side=null: mediumblob
    embedding_bot=null: mediumblob
    embedding_body=null: mediumblob
    """
    
@schema
class CAEEmbeddingAll(dj.Computed):
    definition = """
    -> experiment.SessionTrial
    ---
    embedding_side_all=null: mediumblob
    embedding_bot_all=null: mediumblob
    embedding_body_all=null: mediumblob
    """
    
@schema
class CaeEmbedding32(dj.Imported):
    definition = """
    -> experiment.SessionTrial
    ---
    
    """
    class EmbeddingPart(dj.Part):
        definition = """
        -> master
        part_name: varchar(16) # e.g. side, bot, body
        ---
        embedding_32: longblob
        """

@schema
class CaeEmbeddingOcc(dj.Imported):
    definition = """
    -> experiment.SessionTrial
    ---
    
    """
    class EmbeddingPart(dj.Part):
        definition = """
        -> master
        part_name: varchar(16) # e.g. side, bot, body
        ---
        embedding_occ: longblob
        """

@schema
class LickReset(dj.Computed):
    definition = """
    -> ephys.Unit
    ---
    psth_lick_1: mediumblob
    psth_lick_2: mediumblob
    psth_bins: mediumblob
    peaks_lick_1: mediumblob
    peaks_lick_2: mediumblob
    
    """
    # mtl sessions only
    key_source = experiment.Session & v_tracking.TongueTracking3DBot & experiment.Breathing & v_oralfacial_analysis.MovementTiming & ephys.Unit & 'rig = "RRig-MTL"'
    
    def make(self, key):   
        traces_len=1471
        n_trial=40
        psth_s=-0.4
        psth_e=1
        psth_bin=np.arange(psth_s,psth_e,0.02)
        
        inspir_onset,lick_onset_time,lick_offset_time,ton_onset=(v_oralfacial_analysis.MovementTiming & key).fetch1('inspiration_onset','lick_onset','lick_offset','tongue_onset')
        
        inspir_onset_l=[] # restrict by licking bouts
        for i,val in enumerate(lick_onset_time):
            inspir_onset_l.append(inspir_onset[(inspir_onset>(lick_onset_time[i]+0.2)) & (inspir_onset<(lick_offset_time[i]-0.2))])
        inspir_onset_l=np.array(inspir_onset_l)
        inspir_onset_l=np.hstack(inspir_onset_l)
        
        licks = [] # lick times
        n_licks = [] # number of licks in btw breaths
        ibi = []
        lick_bef_time=[]
        lick2_time = []
        
        max_ibi=np.max(np.diff(inspir_onset))
        
        for i,_ in enumerate(inspir_onset_l[2:-2],2):
            
            lick=ton_onset[(ton_onset > inspir_onset_l[i-2]) & (ton_onset<inspir_onset_l[i+2])]
            
            lick_in=lick[(lick > inspir_onset_l[i]) & (lick<inspir_onset_l[i+1])]
            
            if lick_in.size != 0:  # only include trials with a lick in between the breaths
                lick_bef=lick[(lick < inspir_onset_l[i])] # find the timing of lick before inspiration onset
                if len(lick_bef)>0:
                    lick_bef=lick_bef[-1]
                    licks.append(lick - inspir_onset_l[i])
                    lick2_time.append(lick_in[-1] - inspir_onset_l[i])
                    lick_bef_time.append(lick_bef - inspir_onset_l[i])
                    n_licks.append(lick_in.size)                
                    ibi.append(inspir_onset_l[i+1] - inspir_onset_l[i])
        
        ibi=np.array(ibi)
        n_licks=np.array(n_licks)
        lick_bef_time=np.array(lick_bef_time)
        lick2_time=np.array(lick2_time)
        
        licks[:] = [ele for i, ele in enumerate(licks) if ibi[i]<max_ibi]
        n_licks=n_licks[ibi<max_ibi]
        lick_bef_time=lick_bef_time[ibi<max_ibi]
        lick2_time=lick2_time[ibi<max_ibi]
        ibi=ibi[ibi<max_ibi]
        
        idx_all=np.arange(0,len(ibi))
        lick1_rem=np.where((n_licks==1) & (lick_bef_time>-0.05) & (lick_bef_time<0))
        lick2_rem=np.where((n_licks==2) & (lick2_time>(ibi-0.05)) & (lick2_time<ibi))
        idx_keep=np.setdiff1d(idx_all,np.concatenate((lick1_rem[0],lick2_rem[0])))
        
        licks = [licks[i] for i in idx_keep]
        n_licks=n_licks[idx_keep]
        lick_bef_time=lick_bef_time[idx_keep]
        ibi=ibi[idx_keep]
        
        sorted_indexes=np.argsort(ibi)
        sorted_indexes=sorted_indexes[::-1]
        
        licks = [licks[i] for i in sorted_indexes]
        n_licks=n_licks[sorted_indexes]
        lick_bef_time=lick_bef_time[sorted_indexes]
        ibi=ibi[sorted_indexes]
        
        d_bound=(np.mean(ibi[n_licks==2]) + np.mean(ibi[n_licks==1]))/2
        
        psth_1_i= np.where((ibi<d_bound) & (n_licks==1))[0]
        # psth_2_i= np.where((ibi>d_bound) & (n_licks==2))[0]
        psth_2_i= np.where(n_licks==2)[0]
        if len(psth_1_i)>n_trial:
            psth_1_i=psth_1_i[:n_trial]
        if len(psth_2_i)>n_trial:
            psth_2_i=psth_2_i[-n_trial:]
        
        good_units=ephys.Unit & key & (ephys.UnitPassingCriteria  & 'criteria_passed=1')
        
        unit_keys=good_units.fetch('KEY')
        units_lick=[]
        
        for unit_key in unit_keys:          
            spikes = [] # where the spikes occur
            ibi = []
            lick2_time = []
            n_licks = [] # number of licks in btw breaths
            lick_bef_time=[]
            
            good_trial=(v_tracking.JawTracking3DSid & unit_key).fetch('trial', order_by='trial')
            all_spikes=(ephys.Unit.TrialSpikes & unit_key & [{'trial': tr} for tr in good_trial]).fetch('spike_times')
            good_spikes = all_spikes # get good spikes
            for i, d in enumerate(good_spikes):
                good_spikes[i] = d[d < traces_len*3.4/1000]+traces_len*3.4/1000*i
            good_spikes = np.hstack(good_spikes)
            
            for i,_ in enumerate(inspir_onset_l[2:-2],2):
                
                lick=ton_onset[(ton_onset > inspir_onset_l[i-2]) & (ton_onset<inspir_onset_l[i+2])]
                
                lick_in=lick[(lick > inspir_onset_l[i]) & (lick<inspir_onset_l[i+1])]
                
                if lick_in.size != 0:  # only include trials with a lick in between the breaths
                    lick_bef=lick[(lick < inspir_onset_l[i])] # find the timing of lick before inspiration onset
                    if len(lick_bef)>0:
                        lick_bef=lick_bef[-1]
                        lick2_time.append(lick_in[-1] - inspir_onset_l[i])
                        lick_bef_time.append(lick_bef - inspir_onset_l[i])
                        n_licks.append(lick_in.size)
                        spike_breath=good_spikes-inspir_onset_l[i]
                        spike_breath=spike_breath[spike_breath>psth_s]
                        spike_breath=spike_breath[spike_breath<psth_e]
                        spikes.append(spike_breath)
                        ibi.append(inspir_onset_l[i+1] - inspir_onset_l[i])
        
            lick2_time=np.array(lick2_time)    
            ibi=np.array(ibi)
            lick_bef_time=np.array(lick_bef_time)
            n_licks=np.array(n_licks)
                    
            spikes[:] = [ele for i, ele in enumerate(spikes) if ibi[i]<max_ibi]
            lick2_time=lick2_time[ibi<max_ibi]
            n_licks=n_licks[ibi<max_ibi]
            lick_bef_time=lick_bef_time[ibi<max_ibi]
            ibi=ibi[ibi<max_ibi]
            
            idx_all=np.arange(0,len(ibi))
            lick1_rem=np.where((n_licks==1) & (lick_bef_time>-0.05) & (lick_bef_time<0))
            lick2_rem=np.where((n_licks==2) & (lick2_time>(ibi-0.05)) & (lick2_time<ibi))
            idx_keep=np.setdiff1d(idx_all,np.concatenate((lick1_rem[0],lick2_rem[0])))
        
            spikes = [spikes[i] for i in idx_keep]    
            n_licks=n_licks[idx_keep]
            lick_bef_time=lick_bef_time[idx_keep]
            ibi=ibi[idx_keep]
            
            sorted_indexes=np.argsort(ibi)
            sorted_indexes=sorted_indexes[::-1]
            
            n_licks=n_licks[sorted_indexes]
            lick_bef_time=lick_bef_time[sorted_indexes]
            ibi=ibi[sorted_indexes]
            spikes=[spikes[i] for i in sorted_indexes]
            spikes=[spikes[i]-lbt for i,lbt in enumerate(lick_bef_time)]
            
            psth_lick_1=[spikes[i] for i in psth_1_i]
            psth_lick_1=np.hstack(psth_lick_1)
            psth_lick_1=np.histogram(psth_lick_1,psth_bin)[0]/len(psth_1_i)
            psth_lick_2=[spikes[i] for i in psth_2_i]
            psth_lick_2=np.hstack(psth_lick_2)
            psth_lick_2=np.histogram(psth_lick_2,psth_bin)
            half_bin=(psth_lick_2[1][1]-psth_lick_2[1][0])/2
            psth_bins=psth_lick_2[1][1:]-half_bin
            psth_lick_2=psth_lick_2[0]/len(psth_2_i)/(half_bin*2)
            psth_lick_1=psth_lick_1/(half_bin*2)
            
            psth_1_thr=50#np.mean(psth_lick_1)#+np.std(psth_lick_1)
            peaks_1=signal.find_peaks(psth_lick_1, height=psth_1_thr, distance=0.14/(half_bin*2))[0]
            peaks_lick_1=psth_bins[peaks_1]
            peaks_lick_1=peaks_lick_1[(peaks_lick_1>0.12) & (peaks_lick_1<0.35)]
            psth_2_thr=50#np.mean(psth_lick_2)#+np.std(psth_lick_1)
            peaks_2=signal.find_peaks(psth_lick_2, height=psth_2_thr, distance=0.1/(half_bin*2))[0]
            peaks_lick_2=psth_bins[peaks_2]
            peaks_lick_2=peaks_lick_2[(peaks_lick_2>0.08) & (peaks_lick_2<0.35)]
            
            units_lick.append({**unit_key, 'psth_lick_1': psth_lick_1, 'psth_lick_2': psth_lick_2, 'psth_bins': psth_bins,'peaks_lick_1': peaks_lick_1,'peaks_lick_2': peaks_lick_2})

        self.insert(units_lick, ignore_extra_fields=True)
        
@schema
class UnitPsth(dj.Computed):
    definition = """
    -> ephys.Unit
    ---    
    psth: mediumblob
    psth_bins: mediumblob
    
    """

    key_source = experiment.Session & v_oralfacial_analysis.MovementTiming & ephys.Unit & 'rig = "RRig-MTL"'
    
    def make(self, key):
        traces_len=1471

        good_units=ephys.Unit & key & (ephys.UnitPassingCriteria  & 'criteria_passed=1')

        unit_keys=good_units.fetch('KEY')

        trial_key=(v_tracking.TongueTracking3DBot & key).fetch('trial', order_by='trial')
        lick_onset=(v_oralfacial_analysis.MovementTiming & key).fetch('lick_onset')

        t_before=0.5
        t_after=0.5
        bin_width=0.001
        
        units_psths=[]
        
        for unit_key in unit_keys: # loop for each neuron
            all_spikes=(ephys.Unit.TrialSpikes & unit_key & [{'trial': tr} for tr in trial_key]).fetch('spike_times', order_by='trial')   
            good_spikes=all_spikes # get good spikes
            for i, d in enumerate(good_spikes):
                good_spikes[i] = d[d < traces_len*3.4/1000]+traces_len*3.4/1000*i
            good_spikes = np.hstack(good_spikes)    
            
            psth=[]
            
            for lick_t in lick_onset[0]:
                psth.append(good_spikes[(good_spikes>lick_t-t_before) & (good_spikes<lick_t+t_after)]-lick_t)
            
            psth=np.hstack(psth)
            y, bin_edges = np.histogram(psth, np.arange(-t_before, t_after, bin_width))
            half_bin=(bin_edges[1]-bin_edges[0])/2
            y=y/len(lick_onset[0])/(bin_edges[1]-bin_edges[0])
            units_psths.append({**unit_key, 'psth': y, 'psth_bins': bin_edges[1:]-half_bin})
            
        self.insert(units_psths, ignore_extra_fields=True)
        
@schema
class LickRate(dj.Computed):
    definition = """
    -> ephys.Unit
    ---
    freq_bin: mediumblob
    spike_rate: mediumblob
    fr_slope: float
    fr_intercept: float
    
    """
    # mtl sessions only
    key_source = experiment.Session & v_tracking.TongueTracking3DBot & experiment.Breathing & v_oralfacial_analysis.MovementTiming & ephys.Unit & 'rig = "RRig-MTL"'
    
    def make(self, key):
        traces_len=1471
        inspir_onset,lick_onset_time,lick_offset_time,ton_onset=(v_oralfacial_analysis.MovementTiming & key).fetch1('inspiration_onset','lick_onset','lick_offset','tongue_onset')

        good_units=ephys.Unit & key & (ephys.UnitPassingCriteria  & 'criteria_passed=1')

        unit_keys=good_units.fetch('KEY')
        units_lick_freq=[]

        for unit_key in unit_keys:          
            good_trial=(v_tracking.JawTracking3DSid & unit_key).fetch('trial', order_by='trial')
            all_spikes=(ephys.Unit.TrialSpikes & unit_key & [{'trial': tr} for tr in good_trial]).fetch('spike_times')
            good_spikes = all_spikes # get good spikes
            for i, d in enumerate(good_spikes):
                good_spikes[i] = d[d < traces_len*3.4/1000]+traces_len*3.4/1000*i
            good_spikes = np.hstack(good_spikes)
            
            fr_lick=[] # fr per lick
            ifi = []
            for i,val in enumerate(lick_onset_time):
                bout_ton_onset=ton_onset[(ton_onset>(lick_onset_time[i]+0.2)) & (ton_onset<(lick_offset_time[i]-0.2))]

                for i,_ in enumerate(bout_ton_onset[1:],1):
                    fr_lick.append(len(good_spikes[(good_spikes>(bout_ton_onset[i]-0.025)) & (good_spikes<(bout_ton_onset[i]+0.075))])/0.1)
                    ifi.append(1/(bout_ton_onset[i] - bout_ton_onset[i-1]))

            fr_lick=np.array(fr_lick)
            ifi=np.array(ifi)
            ifi_bins=np.linspace(np.min(ifi),np.max(ifi),10)
            half_bin=(ifi_bins[1]-ifi_bins[0])/2
            freq_bin=ifi_bins[:-1]+half_bin
            
            spike_rate = [np.mean(fr_lick[np.where((ifi > low) & (ifi <= high))]) for low, high in zip(ifi_bins[:-1], ifi_bins[1:])]
            m,b=np.polyfit(freq_bin,spike_rate,1)
            units_lick_freq.append({**unit_key, 'freq_bin': freq_bin, 'spike_rate': spike_rate,'fr_slope': m, 'fr_intercept':b})
        
        self.insert(units_lick_freq, ignore_extra_fields=True)