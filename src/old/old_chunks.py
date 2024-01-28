from snp_generator import SNPGenerator
from peak_generator import PeakGenerator
from utils import argmanager, losses
from utils.helpers import get_gpu_scope
import scipy.stats
from scipy.spatial.distance import jensenshannon
from tensorflow.keras.utils import get_custom_objects
from tensorflow.keras.models import load_model
import tensorflow as tf
import pandas as pd
import os
import argparse
import numpy as np
import h5py
import psutil
from tqdm import tqdm
import statsmodels.stats.multitest
import math


SCHEMA = {'original': ["chr", "pos", "rsid", "allele1", "allele2"],
          'plink': ["chr", "rsid", "ignore1", "pos", "allele1", "allele2"],
          'narrowpeak': ['chr', 'start', 'end', 3, 4, 5, 6, 7, 'rank', 'summit'],
          'bed': ['chr', 'start', 'pos', 'allele1', 'allele2', 'rsid', 'snp_id'],
          'chrombpnet': ["chr", "pos", "allele1", "allele2", "rsid"]}

def main():
    args = argmanager.fetch_scoring_args()
    print(args)

    out_dir = os.path.sep.join(args.out_prefix.split(os.path.sep)[:-1])
    if not os.path.exists(out_dir):
        raise OSError("Output directory does not exist")

    # load the model
    model = load_model_wrapper(args, args.model)

    # load the variants
    variants_table = pd.read_csv(args.list, header=None, sep='\t', names=SCHEMA[args.schema])
    variants_table.drop(columns=[x for x in variants_table.columns if x.startswith('ignore')], inplace=True)
    variants_table['chr'] = variants_table['chr'].astype(str)
    has_chr_prefix = any('chr' in x for x in variants_table['chr'].tolist())
    if not has_chr_prefix:
        variants_table['chr'] = 'chr' + variants_table['chr']

    chrom_sizes = pd.read_csv(args.chrom_sizes, header=None, sep='\t', names=['chrom', 'size'])
    chrom_sizes_dict = chrom_sizes.set_index('chrom')['size'].to_dict()

    if args.chrom:
        variants_table = variants_table.loc[variants_table['chr'] == args.chrom]

    # infer input length
    if args.lite:
        input_len = model.input_shape[0][1]
    else:
        input_len = model.input_shape[1]
    print("input length inferred from the model: ", input_len)

    print(variants_table.shape)
    variants_table = variants_table.loc[variants_table.apply(lambda x: get_valid_variants(x.chr, x.pos, x.allele1, x.allele2, input_len, chrom_sizes_dict), axis=1)]
    print(variants_table.shape)

    if args.debug_mode:
        variants_table = variants_table.sample(100000)
        print(variants_table.head())

    variants_table.reset_index(drop=True, inplace=True)

    if args.peaks:
        if args.peak_chrom_sizes == None:
            args.peak_chrom_sizes = args.chrom_sizes
        if args.peak_genome == None:
            args.peak_genome = args.genome

        peak_chrom_sizes = pd.read_csv(args.peak_chrom_sizes, header=None, sep='\t', names=['chrom', 'size'])
        peak_chrom_sizes_dict = peak_chrom_sizes.set_index('chrom')['size'].to_dict()

        peaks = pd.read_csv(args.peaks, header=None, sep='\t', names=SCHEMA['narrowpeak'])
        peaks.sort_values(by=['chr', 'start', 'end', 'summit', 'rank'], ascending=[True, True, True, True, False], inplace=True)
        peaks.drop_duplicates(subset=['chr', 'start', 'end', 'summit'], inplace=True)

        print(peaks.shape)
        peaks = peaks.loc[peaks.apply(lambda x: get_valid_peaks(x.chr, x.start, x.summit, input_len, peak_chrom_sizes_dict), axis=1)]

        if args.debug_mode:
            peaks = peaks.sample(100000)

        if args.max_peaks:
            if len(peaks) > args.max_peaks:
                peaks = peaks.sample(args.max_peaks)

        peaks.reset_index(drop=True, inplace=True)
        print(peaks.shape)

        count_preds, profile_preds = fetch_peak_predictions(model,
                                                            peaks,
                                                            input_len,
                                                            args.peak_genome,
                                                            args.batch_size,
                                                            debug_mode=args.debug_mode,
                                                            lite=args.lite,
                                                            bias=None)

    for chunk in range(10):
        if not os.path.exists('.'.join([args.out_prefix, str(chunk), "variant_predictions.h5"])):
            variants_chunk = variants_table.iloc[math.ceil(len(variants_table) * (0.1 * chunk)):math.ceil(len(variants_table) * (0.1 * (chunk+1)))].copy()

            if args.max_shuf:
                if len(variants_chunk) > args.max_shuf:
                    shuf_variants_chunk = variants_chunk.sample(args.max_shuf)
                    args.num_shuf = 1
                else:
                    shuf_variants_chunk = variants_chunk.copy()
            else:
                shuf_variants_chunk = variants_chunk.copy()

            print(shuf_variants_chunk.shape)

            # fetch model prediction for variants
            rsids, allele1_count_preds, allele2_count_preds, \
            allele1_profile_preds, allele2_profile_preds = fetch_variant_predictions(model,
                                                                                    variants_chunk,
                                                                                    input_len,
                                                                                    args.genome,
                                                                                    args.batch_size,
                                                                                    debug_mode=args.debug_mode,
                                                                                    lite=args.lite,
                                                                                    bias=None,
                                                                                    shuf=False,
                                                                                    num_shuf=args.num_shuf)

            shuf_rsids, shuf_allele1_count_preds, shuf_allele2_count_preds, \
            shuf_allele1_profile_preds, shuf_allele2_profile_preds = fetch_variant_predictions(model,
                                                                                    shuf_variants_chunk,
                                                                                    input_len,
                                                                                    args.genome,
                                                                                    args.batch_size,
                                                                                    debug_mode=args.debug_mode,
                                                                                    lite=args.lite,
                                                                                    bias=None,
                                                                                    shuf=True,
                                                                                    num_shuf=args.num_shuf)

            if args.peaks:
                log_fold_change, profile_jsd, \
                allele1_percentile, allele2_percentile, \
                percentile_change = get_variant_scores_with_peaks(allele1_count_preds, allele2_count_preds,
                                                                allele1_profile_preds, allele2_profile_preds,
                                                                count_preds)

                shuf_log_fold_change, shuf_profile_jsd, \
                shuf_allele1_percentile, shuf_allele2_percentile, \
                shuf_percentile_change = get_variant_scores_with_peaks(shuf_allele1_count_preds, shuf_allele2_count_preds,
                                                                    shuf_allele1_profile_preds, shuf_allele2_profile_preds,
                                                                    count_preds)

            else:
                log_fold_change, profile_jsd = get_variant_scores(allele1_count_preds, allele2_count_preds,                                                                                                    allele1_profile_preds, allele2_profile_preds)

                shuf_log_fold_change, shuf_profile_jsd = get_variant_scores(shuf_allele1_count_preds, shuf_allele2_count_preds,
                                                                            shuf_allele1_profile_preds, shuf_allele2_profile_preds)

            print(len(shuf_allele1_count_preds))

            # unpack rsids to write outputs and write score to output
            assert np.array_equal(variants_chunk["rsid"].tolist(), rsids)
            variants_chunk["log_fold_change"] = log_fold_change
            variants_chunk["profile_jsd"] = profile_jsd
            variants_chunk["allele1_pred_counts"] = allele1_count_preds
            variants_chunk["allele2_pred_counts"] = allele2_count_preds
            variants_chunk["log_fold_change_pval"] = variants_chunk["log_fold_change"].apply(lambda x:
                                                                                            2 * min(scipy.stats.percentileofscore(shuf_log_fold_change, x) / 100,
                                                                                                    1 - (scipy.stats.percentileofscore(shuf_log_fold_change, x) / 100)))
            variants_chunk["profile_jsd_pval"] = variants_chunk["profile_jsd"].apply(lambda x:
                                                                                    1 - (scipy.stats.percentileofscore(shuf_profile_jsd, x) / 100))
            variants_chunk["poisson_pval"] = variants_chunk.apply(lambda x:
                                                                poisson_pval(x.allele1_pred_counts, x.allele2_pred_counts), axis=1)
            variants_chunk["poisson_pval_best"] = variants_chunk.apply(lambda x:
                                                                    poisson_pval_best(x.allele1_pred_counts, x.allele2_pred_counts), axis=1)
            variants_chunk["poisson_pval_worst"] = variants_chunk.apply(lambda x:
                                                                    poisson_pval_worst(x.allele1_pred_counts, x.allele2_pred_counts), axis=1)
            variants_chunk["poisson_pval_bh"] = statsmodels.stats.multitest.multipletests(variants_chunk["poisson_pval"].tolist(),
                                                                                        method='fdr_bh')[1]

            if args.peaks:
                variants_chunk["allele1_percentile"] = allele1_percentile
                variants_chunk["allele2_percentile"] = allele2_percentile
                variants_chunk["max_percentile"] = variants_chunk[["allele1_percentile", "allele2_percentile"]].max(axis=1)
                variants_chunk["percentile_change"] = percentile_change
                variants_chunk["percentile_change_pval"] = variants_chunk["percentile_change"].apply(lambda x:
                                                                                                2 * min(scipy.stats.percentileofscore(shuf_percentile_change, x) / 100,
                                                                                                        1 - (scipy.stats.percentileofscore(shuf_percentile_change, x) / 100)))

            if args.bias != None:
                bias = load_model_wrapper(args, args.bias)
                w_bias_rsids, w_bias_allele1_count_preds, w_bias_allele2_count_preds, \
                w_bias_allele1_profile_preds, w_bias_allele2_profile_preds = fetch_variant_predictions(model,
                                                                                                    variants_chunk,
                                                                                                    input_len,
                                                                                                    args.genome,
                                                                                                    args.batch_size,
                                                                                                    debug_mode=args.debug_mode,
                                                                                                    lite=args.lite,
                                                                                                    bias=bias,
                                                                                                    shuf=False,
                                                                                                    num_shuf=args.num_shuf)

                shuf_w_bias_rsids, shuf_w_bias_allele1_count_preds, shuf_w_bias_shuf_allele2_count_preds, \
                shuf_w_bias_allele1_profile_preds, shuf_w_bias_allele2_profile_preds = fetch_variant_predictions(model,
                                                                                                                shuf_variants_chunk,
                                                                                                                input_len,
                                                                                                                args.genome,
                                                                                                                args.batch_size,
                                                                                                                debug_mode=args.debug_mode,
                                                                                                                lite=args.lite,
                                                                                                                bias=bias,
                                                                                                                shuf=True,
                                                                                                                num_shuf=args.num_shuf)

                if args.peaks:
                    w_bias_count_preds, w_bias_profile_preds = fetch_peak_predictions(model,
                                                                                    peaks,
                                                                                    input_len,
                                                                                    args.peak_genome,
                                                                                    args.batch_size,
                                                                                    debug_mode=args.debug_mode,
                                                                                    lite=args.lite,
                                                                                    bias=bias)

                    w_bias_log_fold_change, w_bias_profile_jsd, \
                    w_bias_allele1_percentile, w_bias_allele2_percentile, \
                    w_bias_percentile_change = get_variant_scores(w_bias_allele1_count_preds, w_bias_allele2_count_preds,
                                                                w_bias_allele1_profile_preds, w_bias_allele2_profile_preds,
                                                                w_bias_count_preds)

                    shuf_w_bias_log_fold_change, shuf_w_bias_profile_jsd, \
                    shuf_w_bias_allele1_percentile, shuf_w_bias_allele2_percentile, \
                    shuf_w_bias_percentile_change = get_variant_scores(shuf_w_bias_allele1_count_preds, shuf_w_bias_shuf_allele2_count_preds,
                                                                    shuf_w_bias_allele1_profile_preds, shuf_w_bias_allele2_profile_preds,
                                                                    w_bias_count_preds)
                else:
                    w_bias_log_fold_change, w_bias_profile_jsd = get_variant_scores(w_bias_allele1_count_preds, w_bias_allele2_count_preds,
                                                                w_bias_allele1_profile_preds, w_bias_allele2_profile_preds)

                    shuf_w_bias_log_fold_change, shuf_w_bias_profile_jsd = get_variant_scores(shuf_w_bias_allele1_count_preds,
                                                                                            shuf_w_bias_shuf_allele2_count_preds,
                                                                                            shuf_w_bias_allele1_profile_preds,
                                                                                            shuf_w_bias_allele2_profile_preds)

                assert np.array_equal(variants_chunk["rsid"].tolist(), w_bias_rsids)
                variants_chunk["log_fold_change_w_bias"] = w_bias_log_fold_change
                variants_chunk["profile_jsd_w_bias"] = w_bias_profile_jsd
                variants_chunk["allele1_pred_counts_w_bias"] = w_bias_allele1_count_preds
                variants_chunk["allele2_pred_counts_w_bias"] = w_bias_allele2_count_preds
                variants_chunk["log_fold_change_w_bias_pval"] = variants_chunk["log_fold_change_w_bias"].apply(lambda x:
                                                                                                2 * min(scipy.stats.percentileofscore(shuf_w_bias_log_fold_change, x) / 100,
                                                                                                        1 - (scipy.stats.percentileofscore(shuf_w_bias_log_fold_change, x) / 100)))
                variants_chunk["profile_jsd_w_bias_pval"] = variants_chunk["profile_jsd_w_bias"].apply(lambda x:
                                                                                        1 - (scipy.stats.percentileofscore(shuf_w_bias_profile_jsd, x) / 100))
                variants_chunk["poisson_w_bias_pval"] = variants_chunk.apply(lambda x:
                                                                    poisson_pval(x.allele1_pred_counts_w_bias, x.allele2_pred_counts_w_bias), axis=1)

                if args.peaks:
                    variants_chunk["allele1_percentile_w_bias"] = w_bias_allele1_percentile
                    variants_chunk["allele2_percentile_w_bias"] = w_bias_allele2_percentile
                    variants_chunk["max_percentile_w_bias"] = variants_chunk[["allele1_percentile_w_bias", "allele2_percentile_w_bias"]].max(axis=1)
                    variants_chunk["percentile_change_w_bias"] = w_bias_percentile_change
                    variants_chunk["percentile_change_w_bias_pval"] = variants_chunk["percentile_change_w_bias"].apply(lambda x:
                                                                                                1 - (scipy.stats.percentileofscore(shuf_w_bias_percentile_change, x) / 100))

            variants_chunk.to_csv('.'.join([args.out_prefix, str(chunk), "variant_scores.tsv"]), sep="\t", index=False)

            # store predictions at variants
            with h5py.File('.'.join([args.out_prefix, str(chunk), "variant_predictions.h5"]), 'w') as f:
                wo_bias = f.create_group('wo_bias')
                wo_bias.create_dataset('allele1_pred_counts', data=allele1_count_preds)
                wo_bias.create_dataset('allele2_pred_counts', data=allele2_count_preds)
                wo_bias.create_dataset('allele1_pred_profile', data=allele1_profile_preds)
                wo_bias.create_dataset('allele2_pred_profile', data=allele2_profile_preds)
                wo_bias.create_dataset('shuf_log_fold_change', data=shuf_log_fold_change)
                wo_bias.create_dataset('shuf_profile_jsd', data=shuf_profile_jsd)
                if args.peaks:
                    wo_bias.create_dataset('shuf_percentile_change', data=shuf_percentile_change)

                if args.bias != None:
                    w_bias = f.create_group('w_bias')
                    w_bias.create_dataset('allele1_pred_counts_w_bias', data=w_bias_allele1_count_preds)
                    w_bias.create_dataset('allele2_pred_counts_w_bias', data=w_bias_allele2_count_preds)
                    w_bias.create_dataset('allele1_pred_profile_w_bias', data=w_bias_allele1_profile_preds)
                    w_bias.create_dataset('allele2_pred_profile_w_bias', data=w_bias_allele2_profile_preds)
                    wo_bias.create_dataset('shuf_w_bias_log_fold_change', data=shuf_w_bias_log_fold_change)
                    wo_bias.create_dataset('shuf_w_bias_profile_jsd', data=shuf_w_bias_profile_jsd)
                    if args.peaks:
                        wo_bias.create_dataset('shuf_w_bias_percentile_change', data=shuf_w_bias_percentile_change)

            print("DONE")

def poisson_pval(allele1_counts, allele2_counts):
    if allele2_counts > allele1_counts:
        pval = 1 - scipy.stats.poisson.cdf(allele2_counts, allele1_counts)
    else:
        pval = scipy.stats.poisson.cdf(allele2_counts, allele1_counts)
    pval = pval * 2
    return pval

def poisson_pval_best(allele1_counts, allele2_counts):
    if allele2_counts > allele1_counts:
        pval_1 = 1 - scipy.stats.poisson.cdf(allele2_counts, allele1_counts)
        pval_2 = scipy.stats.poisson.cdf(allele1_counts, allele2_counts)
    else:
        pval_1 = scipy.stats.poisson.cdf(allele2_counts, allele1_counts)
        pval_2 = 1 - scipy.stats.poisson.cdf(allele1_counts, allele2_counts)
    pval = min([pval_1, pval_2]) * 2
    return pval

def poisson_pval_worst(allele1_counts, allele2_counts):
    if allele2_counts > allele1_counts:
        pval_1 = 1 - scipy.stats.poisson.cdf(allele2_counts, allele1_counts)
        pval_2 = scipy.stats.poisson.cdf(allele1_counts, allele2_counts)
    else:
        pval_1 = scipy.stats.poisson.cdf(allele2_counts, allele1_counts)
        pval_2 = 1 - scipy.stats.poisson.cdf(allele1_counts, allele2_counts)
    pval = max([pval_1, pval_2]) * 2
    return pval

def get_valid_peaks(chrom, pos, summit, input_len, chrom_sizes_dict):
    flank = input_len // 2
    lower_check = ((pos + summit) - flank > 0)
    upper_check = ((pos + summit) + flank <= chrom_sizes_dict[chrom])
    in_bounds = lower_check and upper_check
    return in_bounds

def get_valid_variants(chrom, pos, allele1, allele2, input_len, chrom_sizes_dict):
    flank = input_len // 2
    lower_check = (pos - flank > 0)
    upper_check = (pos + flank <= chrom_sizes_dict[chrom])
    in_bounds = lower_check and upper_check
    no_allele1_indel = (len(allele1) == 1)
    no_allele2_indel = (len(allele2) == 1)
    no_indels = no_allele1_indel and no_allele2_indel
    valid_variants = in_bounds and no_indels
    return valid_variants

def softmax(x, temp=1):
    norm_x = x - np.mean(x,axis=1, keepdims=True)
    return np.exp(temp*norm_x)/np.sum(np.exp(temp*norm_x), axis=1, keepdims=True)

def load_model_wrapper(args, model_file):
    # read .h5 model
    custom_objects = {"multinomial_nll": losses.multinomial_nll, "tf": tf}
    get_custom_objects().update(custom_objects)
    with get_gpu_scope(args):
        model = load_model(model_file, compile=False)
    print("model loaded succesfully")
    return model

def fetch_peak_predictions(model, peaks, input_len, genome_fasta, batch_size, debug_mode=False, lite=False, bias=None):
    count_preds = []
    profile_preds = []

    # peak sequence generator
    peak_gen = PeakGenerator(peaks=peaks,
                             input_len=input_len,
                             genome_fasta=genome_fasta,
                             batch_size=batch_size,
                             debug_mode=debug_mode)

    for i in tqdm(range(len(peak_gen))):

        seqs = peak_gen[i]

        if lite:
            if bias != None:
                bias_batch_preds = bias.predict(seqs, verbose=False)

                batch_preds = model.predict([seqs,
                                             bias_batch_preds[0],
                                             bias_batch_preds[1]],
                                            verbose=False)
            else:
                batch_preds = model.predict([seqs,
                                             np.zeros((len(seqs), model.output_shape[0][1])),
                                             np.zeros((len(seqs), ))],
                                            verbose=False)
        else:
            if bias != None:
                batch_preds = bias.predict(seqs, verbose=False)
            else:
                batch_preds = model.predict(seqs, verbose=False)

        count_preds.extend(np.exp(np.squeeze(batch_preds[1])) - 1)
        profile_preds.extend(np.squeeze(softmax(batch_preds[0])))

    return np.array(count_preds), np.array(profile_preds)

def fetch_variant_predictions(model, variants_table, input_len, genome_fasta, batch_size, debug_mode=False, lite=False, bias=None, shuf=False, num_shuf=10):
    rsids = []
    allele1_count_preds = []
    allele2_count_preds = []
    allele1_profile_preds = []
    allele2_profile_preds = []

    # snp sequence generator
    snp_gen = SNPGenerator(variants_table=variants_table,
                           input_len=input_len,
                           genome_fasta=genome_fasta,
                           batch_size=batch_size,
                           debug_mode=debug_mode,
                           shuf=shuf,
                           num_shuf=num_shuf)

    print()

    for i in tqdm(range(len(snp_gen))):

        batch_rsids, allele1_seqs, allele2_seqs = snp_gen[i]

        if lite:
            if bias != None:
                allele1_bias_batch_preds = bias.predict(allele1_seqs, verbose=False)
                allele2_bias_batch_preds = bias.predict(allele2_seqs, verbose=False)

                allele1_batch_preds = model.predict([allele1_seqs,
                                                     allele1_bias_batch_preds[0],
                                                     allele1_bias_batch_preds[1]],
                                                    verbose=False)
                allele2_batch_preds = model.predict([allele2_seqs,
                                                     allele2_bias_batch_preds[0],
                                                     allele2_bias_batch_preds[1]],
                                                    verbose=False)
            else:
                allele1_batch_preds = model.predict([allele1_seqs,
                                                     np.zeros((len(allele1_seqs), model.output_shape[0][1])),
                                                     np.zeros((len(allele1_seqs), ))],
                                                    verbose=False)
                allele2_batch_preds = model.predict([allele2_seqs,
                                                     np.zeros((len(allele2_seqs), model.output_shape[0][1])),
                                                     np.zeros((len(allele2_seqs), ))],
                                                    verbose=False)
        else:
            if bias != None:
                allele1_batch_preds = bias.predict(allele1_seqs, verbose=False)
                allele2_batch_preds = bias.predict(allele2_seqs, verbose=False)
            else:
                allele1_batch_preds = model.predict(allele1_seqs, verbose=False)
                allele2_batch_preds = model.predict(allele2_seqs, verbose=False)

        #for index,item in enumerate(allele1_batch_preds):
        #    print(item)

        #print(type(allele1_batch_preds))

        allele1_batch_preds[1] = np.array([allele1_batch_preds[1][i] for i in range(len(allele1_batch_preds[1]))])
        allele2_batch_preds[1] = np.array([allele2_batch_preds[1][i] for i in range(len(allele2_batch_preds[1]))])

        #print(allele1_seqs.shape)
        #print(allele1_batch_preds[1].shape)
        #print()
        #print(allele2_seqs.shape)
        #print(allele2_batch_preds[1].shape)
        #print()

        allele1_count_preds.extend(np.exp(allele1_batch_preds[1]))
        allele2_count_preds.extend(np.exp(allele2_batch_preds[1]))

        allele1_profile_preds.extend(np.squeeze(softmax(allele1_batch_preds[0])))
        allele2_profile_preds.extend(np.squeeze(softmax(allele2_batch_preds[0])))

        rsids.extend(batch_rsids)

    return np.array(rsids), np.array(allele1_count_preds), np.array(allele2_count_preds), \
           np.array(allele1_profile_preds), np.array(allele2_profile_preds)

def get_variant_scores_with_peaks(allele1_count_preds, allele2_count_preds,
                       allele1_profile_preds, allele2_profile_preds, count_preds):
    log_fold_change = np.log2(allele2_count_preds / allele1_count_preds)
    profile_jsd_diff = np.array([jensenshannon(x,y) for x,y in zip(allele2_profile_preds, allele1_profile_preds)])
    allele1_percentile = np.array([np.mean(count_preds < x) for x in allele1_count_preds])
    allele2_percentile = np.array([np.mean(count_preds < x) for x in allele2_count_preds])
    percentile_change = allele2_percentile - allele1_percentile

    return log_fold_change, profile_jsd_diff, allele1_percentile, allele2_percentile, percentile_change

def get_variant_scores(allele1_count_preds, allele2_count_preds,
                       allele1_profile_preds, allele2_profile_preds):
    log_fold_change = np.log2(allele2_count_preds / allele1_count_preds)
    profile_jsd_diff = np.array([jensenshannon(x,y) for x,y in zip(allele2_profile_preds, allele1_profile_preds)])

    return log_fold_change, profile_jsd_diff

if __name__ == "__main__":
    main()




