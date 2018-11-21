'''
Created on Jan 8, 2018

@author: simpson
'''

import os
import numpy as np
from compute_metrics import load_results_data, get_fold_data
import pickle
from sklearn.metrics.classification import accuracy_score
import matplotlib.pyplot as plt
import compute_metrics

figure_save_path = './documents/pref_learning_for_convincingness/figures/scalability'
if not os.path.isdir(figure_save_path):
    os.mkdir(figure_save_path)

if __name__ == '__main__':

    # The first part loads runtimes and accuracies with varying no. inducing points

    if 'expt_settings' not in globals():
        expt_settings = {}
        expt_settings['dataset'] = None
        expt_settings['folds'] = None 
    expt_settings['foldorderfile'] = None    
    data_root_dir = os.path.expanduser("~/data/personalised_argumentation/")
    resultsfile_template = 'habernal_%s_%s_%s_%s_acc%.2f_di%.2f'
        
    expt_settings['dataset'] = 'UKPConvArgStrict'
    expt_settings['acc'] = 1.0
    expt_settings['di'] = 0.0
    compute_metrics.max_no_folds = 32
        
    # Create a plot for the runtime/accuracy against M + include other methods with ling + Glove features
    methods =  ['SinglePrefGP_noOpt_weaksprior_M2',
                'SinglePrefGP_noOpt_weaksprior_M10',
                'SinglePrefGP_noOpt_weaksprior_M100',
                'SinglePrefGP_noOpt_weaksprior_M200',
                'SinglePrefGP_noOpt_weaksprior_M300',
                'SinglePrefGP_noOpt_weaksprior_M400',
                'SinglePrefGP_noOpt_weaksprior_M500',
                'SinglePrefGP_noOpt_weaksprior_M600',
                'SinglePrefGP_noOpt_weaksprior_M700',
                'PersPrefGP_commonmean_noOpt_M2',
                'PersPrefGP_commonmean_noOpt_M10',
                'PersPrefGP_commonmean_noOpt_M100',
                'PersPrefGP_commonmean_noOpt_M200',
                'PersPrefGP_commonmean_noOpt_M300',
                'PersPrefGP_commonmean_noOpt_M400',
                'PersPrefGP_commonmean_noOpt_M500',
                'PersPrefGP_commonmean_noOpt_M600',
                'PersPrefGP_commonmean_noOpt_M700',
                'SVM', 'BI-LSTM', #'SinglePrefGP_weaksprior',
                ]


    # Load the results for accuracy and runtime vs. no. inducing points with both feature sets
    expt_settings['feature_type'] = 'both'
    expt_settings['embeddings_type'] = 'word_mean'
    
    docids = None
    
    dims_methods = np.array(['SinglePrefGP_noOpt_weaksprior_M500', 'PersPrefGP_commonmean_noOpt_M500', 'SVM', 'BI-LSTM'])
    #, 'SinglePrefGP_weaksprior'])
    runtimes_dims = np.zeros((len(dims_methods), 4))
    
    runtimes_both = np.zeros(len(methods))
    acc_both = np.zeros(len(methods))
    
    for m, expt_settings['method'] in enumerate(methods): 
        print("Processing method %s" % expt_settings['method'])

        data, nFolds, resultsdir, resultsfile = load_results_data(data_root_dir, resultsfile_template, 
                                                                          expt_settings)
        
        acc_m = np.zeros(nFolds)
        runtimes_m = np.zeros(nFolds)
        
        for f in range(nFolds):
            print("Processing fold %i" % f)
            if expt_settings['fold_order'] is None: # fall back to the order on the current machine
                if expt_settings['folds'] is None:
                    continue
                fold = list(expt_settings['folds'].keys())[f]
            else:
                fold = expt_settings['fold_order'][f] 
                if fold[-2] == "'" and fold[0] == "'":
                    fold = fold[1:-2]
                elif fold[-1] == "'" and fold[0] == "'":
                    fold = fold[1:-1]  
                expt_settings['fold_order'][f] = fold
                                          
            # look for new-style data in separate files for each fold. Prefer new-style if both are found.
            foldfile = resultsdir + '/fold%i.pkl' % f
            if os.path.isfile(foldfile):
                with open(foldfile, 'rb') as fh:
                    data_f = pickle.load(fh, encoding='latin1')
            else: # convert the old stuff to new stuff
                if data is None:
                    min_folds = f+1
                    print('Skipping fold with no data %i' % f)
                    print("Skipping results for %s, %s, %s, %s" % (expt_settings['method'], 
                                                                   expt_settings['dataset'], 
                                                                   expt_settings['feature_type'], 
                                                                   expt_settings['embeddings_type']))
                    print("Skipped filename was: %s, old-style results file would be %s" % (foldfile, 
                                                                                            resultsfile))
                    continue
                
                if not os.path.isdir(resultsdir):
                    os.mkdir(resultsdir)
                data_f = []
                for thing in data:
                    if f in thing:
                        data_f.append(thing[f])
                    else:
                        data_f.append(thing)
                with open(foldfile, 'wb') as fh:
                    pickle.dump(data_f, fh)  
                              
            gold_disc, pred_disc, gold_prob, pred_prob, gold_rank, pred_rank, pred_tr_disc, \
                                        pred_tr_prob, postprocced = get_fold_data(data_f, f, expt_settings)
                                        
            acc_m[f] = accuracy_score(gold_disc[gold_disc!=1], pred_disc[gold_disc!=1])
            runtimes_m[f] = data_f[6]
            
        acc_m = acc_m[acc_m>0]
        runtimes_m = runtimes_m[runtimes_m>0]            
            
        if len(acc_m):
            acc_both[m] = np.mean(acc_m)
            runtimes_both[m] = np.mean(runtimes_m)
            if expt_settings['method'] in dims_methods:
                m_dims = dims_methods == expt_settings['method']
                runtimes_dims[m_dims, 3] = runtimes_both[m]

    # Load the results for accuracy and runtime vs. no. inducing points with embeddings
    expt_settings['feature_type'] = 'embeddings'
        
    runtimes_emb = np.zeros(len(methods))
    acc_emb = np.zeros(len(methods))
    
    for m, expt_settings['method'] in enumerate(methods): 
        print("Processing method %s" % expt_settings['method'])

        data, nFolds, resultsdir, resultsfile = load_results_data(data_root_dir, resultsfile_template, 
                                                                          expt_settings)
        
        acc_m = np.zeros(nFolds)
        runtimes_m = np.zeros(nFolds)
        
        for f in range(nFolds):
            print("Processing fold %i" % f)
            if expt_settings['fold_order'] is None: # fall back to the order on the current machine
                if expt_settings['folds'] is None:
                    continue
                fold = list(expt_settings['folds'].keys())[f]
            else:
                fold = expt_settings['fold_order'][f] 
                if fold[-2] == "'" and fold[0] == "'":
                    fold = fold[1:-2]
                elif fold[-1] == "'" and fold[0] == "'":
                    fold = fold[1:-1]  
                expt_settings['fold_order'][f] = fold
                                                          
            # look for new-style data in separate files for each fold. Prefer new-style if both are found.
            foldfile = resultsdir + '/fold%i.pkl' % f
            if os.path.isfile(foldfile):
                with open(foldfile, 'rb') as fh:
                    data_f = pickle.load(fh, encoding='latin1')
            else: # convert the old stuff to new stuff
                if data is None:
                    min_folds = f+1
                    print('Skipping fold with no data %i' % f)
                    print("Skipping results for %s, %s, %s, %s" % (expt_settings['method'], 
                                                                   expt_settings['dataset'], 
                                                                   expt_settings['feature_type'], 
                                                                   expt_settings['embeddings_type']))
                    print("Skipped filename was: %s, old-style results file would be %s" % (foldfile, 
                                                                                            resultsfile))
                    continue
                
                if not os.path.isdir(resultsdir):
                    os.mkdir(resultsdir)
                data_f = []
                for thing in data:
                    if f in thing:
                        data_f.append(thing[f])
                    else:
                        data_f.append(thing)
                with open(foldfile, 'wb') as fh:
                    pickle.dump(data_f, fh)  
                 
            fold = expt_settings['fold_order'][f]
            if fold[-2] == "'" and fold[0] == "'":
                fold = fold[1:-2]
            elif fold[-1] == "'" and fold[0] == "'":
                fold = fold[1:-1]  
            expt_settings['fold_order'][f] = fold
                         
            gold_disc, pred_disc, gold_prob, pred_prob, gold_rank, pred_rank, pred_tr_disc, \
                                        pred_tr_prob, postprocced = get_fold_data(data_f, f, expt_settings)
                                        
            acc_m[f] = accuracy_score(gold_disc[gold_disc!=1], pred_disc[gold_disc!=1])
            runtimes_m[f] = data_f[6]
            
        acc_m = acc_m[acc_m>0]
        runtimes_m = runtimes_m[runtimes_m>0]
                
        if len(acc_m):
            acc_emb[m] = np.mean(acc_m)
            runtimes_emb[m] = np.mean(runtimes_m)
            if expt_settings['method'] in dims_methods:
                m_dims = dims_methods == expt_settings['method']
                runtimes_dims[m_dims, 1] = runtimes_emb[m]          
        
    # First plot: M versus Runtime and Accuracy for 32310 features -----------------------------------------------------
    
    fig1, ax1 = plt.subplots(figsize=(5,4))
    x_gppl = np.array([2, 10, 100, 200, 300, 400, 500, 600, 700])
    h1, = ax1.plot(x_gppl, runtimes_both[:-3], color='blue', marker='o', label='runtime', 
                   linewidth=2, markersize=8)  
    ax1.set_ylabel('Runtime (s)')
    plt.xlabel('No. Inducing Points, M')
    ax1.grid('on', axis='y')
    ax1.spines['left'].set_color('blue')
    ax1.tick_params('y', colors='blue')
    ax1.yaxis.label.set_color('blue')
    
    ax1_2 = ax1.twinx()
    h2, = ax1_2.plot(x_gppl, acc_both[:-3], color='black', marker='x', label='accuracy', 
                     linewidth=2, markersize=8)
    #ax1_2.set_ylabel('Accuracy')
    leg = plt.legend(handles=[h1, h2], loc='lower right')
    leg.get_texts()[0].set_color('blue')
    
    plt.tight_layout()    
    plt.savefig(figure_save_path + '/num_inducing_32310_features.pdf')        
    
    # Second plot: M versus runtime and accuracy for 300 features ------------------------------------------------------

    fig1, ax2 = plt.subplots(figsize=(5,4))
    h1, = ax2.plot(x_gppl, runtimes_emb[:-3], color='blue', marker='o', label='runtime', 
                   linewidth=2, markersize=8)
    plt.xlabel('No. Inducing Points, M')
    ax2.grid('on', axis='y')   
    #ax2.set_ylabel('Runtime (s)')
    ax2.spines['left'].set_color('blue')
    ax2.tick_params('y', colors='blue')
    ax2.yaxis.label.set_color('blue')    
            
    ax2_2 = ax2.twinx()
    h2, = ax2_2.plot(x_gppl, acc_emb[:-3], color='black', marker='x', label='accuracy', 
                     linewidth=2, markersize=8)
    ax2_2.set_ylabel('Accuracy')
    leg = plt.legend(handles=[h1, h2], loc='lower right')
    leg.get_texts()[0].set_color('blue')
    
    plt.tight_layout()    
    plt.savefig(figure_save_path + '/num_inducing_300_features.pdf')    
