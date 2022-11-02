#!/usr/bin/python3
"""Cluster UMIs."""
# This code makes significant use of the UMI-tools package (MIT license).
#
# https://github.com/CGATOxford/UMI-tools
# https://genome.cshlp.org/content/early/2017/01/18/gr.209601.116.abstract
#
# The specific functions borrowed or modified are documented below in comments

import argparse
import collections
import itertools
import logging
import multiprocessing
from pathlib import Path
import tempfile

from editdistance import eval as edit_distance
import numpy as np
import pandas as pd
import pysam
from tqdm import tqdm


logger = logging.getLogger(__name__)


def parse_args():
    """Create argument parser."""
    parser = argparse.ArgumentParser()

    # Mandatory arguments
    parser.add_argument(
        "bam",
        help="BAM file of alignments with tags for gene (GN), \
        corrected barcode (CB) and uncorrected UMI (UY)",
        type=Path,
    )

    parser.add_argument(
        "--chrom",
        help="Chromosome name",
    )

    # Optional arguments
    parser.add_argument(
        "--output",
        help="Output BAM file with new tags for corrected UMI (UB) \
        [tagged.sorted.bam]",
        type=Path,
        default=Path("tagged.sorted.bam"),
    )

    parser.add_argument(
        "--output_read_tags",
        help="Output file for read to tag TSV [read_tags.tsv]",
        type=Path,
        default=Path("read_tags.tsv"),
    )

    parser.add_argument(
        "-i",
        "--ref_interval",
        help="Size of genomic window (bp) to assign as gene name if no gene \
        assigned by featureCounts [1000]",
        type=int,
        default=1000,
    )

    parser.add_argument(
        "--cell_gene_max_reads",
        help="Maximum number of reads to consider for a particular \
        gene + cell barcode combination. \
        This is required to prevent too many PCR \
        duplicates from crashing the UMI clustering algorithm. \
        Can be increased \
        if sufficient UMI complexity is observed. [20000]",
        type=int,
        default=20000
    )

    parser.add_argument(
        "--gene_assigns",
        help="TSV read/gene assignments file. \
        IMPORTANT: reads in the input BAM and gene_assigns file must have the \
        same order.",
        type=Path,
    ),

    parser.add_argument(
        "--transcript_assigns",
        help="TSV read/transcript assignments file. \
        IMPORTANT: reads in the input BAM and gene_assigns file must have the \
        same order.",
        type=Path,
    ),

    parser.add_argument(
        "--bc_ur_tags",
        help="TSV read/BC/UR tag assignments file. \
        IMPORTANT: reads in the input BAM and gene_assigns file must have the \
        same order? .",
        type=Path,
    ),

    parser.add_argument(
        "-t", "--threads", help="Threads to use [4]", type=int, default=4
    )

    parser.add_argument(
        "--verbosity",
        help="logging level: <=2 logs info, <=3 logs warnings",
        type=int,
        default=2,
    )

    # Parse arguments
    args = parser.parse_args()

    # Create temp dir and add that to the args object
    p = Path(args.output)
    tempdir = tempfile.TemporaryDirectory(prefix="tmp.", dir=p.parents[0])
    args.tempdir = tempdir.name

    return args


def init_logger(args):
    """Initiate logger."""
    logging.basicConfig(
        format="%(asctime)s -- %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    logging_level = args.verbosity * 10
    logging.root.setLevel(logging_level)
    logging.root.handlers[0].addFilter(lambda x: "NumExpr" not in x.msg)


def breadth_first_search(node, adj_list):
    """Breadth first search.

    This function has been copied from the UMI-tools package, originally found
    in the networks.py source code here:
    https://github.com/CGATOxford/UMI-tools/blob/c3ead0792ad590822ca72239ef01b8e559802da9/umi_tools/network.py#L21
    """
    searched = set()
    queue = set()
    queue.update((node,))
    searched.update((node,))

    while len(queue) > 0:
        node = queue.pop()
        for next_node in adj_list[node]:
            if next_node not in searched:
                queue.update((next_node,))
                searched.update((next_node,))

    return searched


def get_adj_list_directional(umis, counts, threshold=2):
    """
    Identify all umis within the LEVENSHTEIN distance threshold.

    Also where the counts of the first umi is > (2 * second umi counts)-1.

    This function from UMI-tools has been modified to use Levenshtein distance
    instead of hamming distance.

    This function has been modified from the UMI-tools package,
    originally found in the network.py source code here:
    https://github.com/CGATOxford/UMI-tools/blob/c3ead0792ad590822ca72239ef01b8e559802da9/umi_tools/network.py#L187
    """
    adj_list = {umi: [] for umi in umis}
    iter_umi_pairs = itertools.combinations(umis, 2)
    for umi1, umi2 in iter_umi_pairs:
        if edit_distance(umi1, umi2) <= threshold:
            if counts[umi1] >= (counts[umi2] * 2) - 1:
                adj_list[umi1].append(umi2)
            if counts[umi2] >= (counts[umi1] * 2) - 1:
                adj_list[umi2].append(umi1)

    return adj_list


def get_connected_components_adjacency(umis, graph, counts):
    """
    Find the connected UMIs within an adjacency dictionary.

    This function has been copied from the UMI-tools package, originally found
    in the network.py source code here:
    https://github.com/CGATOxford/UMI-tools/blob/c3ead0792ad590822ca72239ef01b8e559802da9/umi_tools/network.py#L213
    """
    # TS: TO DO: Work out why recursive function doesn't lead to same
    # final output. Then uncomment below

    # if len(graph) < 10000:
    #    self.search = breadth_first_search_recursive
    # else:
    #    self.search = breadth_first_search

    found = set()
    components = list()

    for node in sorted(graph, key=lambda x: counts[x], reverse=True):
        if node not in found:
            # component = self.search(node, graph)
            component = breadth_first_search(node, graph)
            found.update(component)
            components.append(component)
    return components


def group_directional(clusters, adj_list, counts):
    """
    Return groups for directional method.

    This function has been copied from the UMI-tools package, originally found
    in the network.py source code here:
    https://github.com/CGATOxford/UMI-tools/blob/c3ead0792ad590822ca72239ef01b8e559802da9/umi_tools/network.py#L250
    """
    observed = set()
    groups = []
    for cluster in clusters:
        if len(cluster) == 1:
            groups.append(list(cluster))
            observed.update(cluster)
        else:
            cluster = sorted(cluster, key=lambda x: counts[x], reverse=True)
            # need to remove any node which has already been observed
            temp_cluster = []
            for node in cluster:
                if node not in observed:
                    temp_cluster.append(node)
                    observed.add(node)
            groups.append(temp_cluster)

    return groups


def cluster(counts_dict, threshold=3):
    """Cluster."""
    adj_list = get_adj_list_directional(
        counts_dict.keys(), counts_dict, threshold)
    clusters = get_connected_components_adjacency(
        counts_dict.keys(), adj_list, counts_dict
    )
    final_umis = [
        list(x) for x in group_directional(
            clusters,
            adj_list,
            counts_dict)]
    return final_umis


def create_map_to_correct_umi(cluster_list):
    """Create map to correct umi."""
    my_map = {y: x[0] for x in cluster_list for y in x}
    return my_map


def correct_umis(umis):
    """Correct Umis."""
    counts_dict = dict(umis.value_counts())
    umi_map = create_map_to_correct_umi(cluster(counts_dict))
    return umis.replace(umi_map)


def add_tags(chrom, umis, genes, transcripts, args):
    """
    Add tags.

    Using the read_id:umi_corr and read_id:gene dictionaries, add UB:Z and GN:Z
    tags to the output BAM file.
    """
    bam_out_fn = args.output
    read_tags = []

    with pysam.AlignmentFile(args.bam, "rb") as bam:
        with pysam.AlignmentFile(bam_out_fn, "wb", template=bam) as bam_out:

            for align in bam.fetch(chrom):
                read_id = align.query_name

                if (umis.get(read_id) is not None) & \
                        (genes.get(read_id) is not None):
                    # Corrected UMI = UB:Z
                    align.set_tag("UB", umis[read_id], value_type="Z")

                    # Annotated gene name = GN:Z
                    align.set_tag("GN", genes[read_id], value_type="Z")

                    # Annotated transwcript name = TR:Z
                    align.set_tag("TR", transcripts[read_id], value_type="Z")

                    bam_out.write(align)
                    read_tags.append(
                        [read_id,
                         align.get_tag("GN"),
                         align.get_tag("TR"),
                         align.get_tag("CB"),
                         align.get_tag("UB")])

    header = ['read_id', 'gene', 'transcript', 'barcode', 'umi']
    return pd.DataFrame.from_records(read_tags, columns=header)


def get_bam_info(bam):
    """
    Get bam info.

    Use `samtools idxstat` to get number of alignments and names of all contigs
    in the reference.

    :param bam: Path to sorted BAM file
    :type bame: str
    :return: Sum of all alignments in the BAM index file and list of all chroms
    :rtype: int,list
    """
    with pysam.AlignmentFile(bam, "rb") as bam:
        stats = bam.get_index_statistics()
        n_aligns = int(sum([contig.mapped for contig in stats]))
        chroms = dict(
            [(contig.contig, contig.mapped)
                for contig in stats if contig.mapped > 0])

    return n_aligns, chroms


def create_region_name(read, args):
    """
    Create region name.

    Create a 'gene name' based on the aligned chromosome and coordinates.
    The midpoint of the alignment determines which genomic interval to use
    for the 'gene name'.

    :param read: read tags and location
    :type read: tuple
    :param args: object containing all supplied arguments
    :type args: class 'argparse.Namespace'
    :return: Newly created 'gene name' based on aligned chromosome and coords
    :rtype: str
    """
    chrom = read.chr
    start_pos = read.start
    end_pos = read.end

    # Find the midpoint of the alignment
    midpoint = int((start_pos + end_pos) / 2)

    # Pick the genomic interval based on this alignment midpoint
    interval_start = np.floor(midpoint / args.ref_interval) * args.ref_interval
    interval_end = np.ceil(midpoint / args.ref_interval) * args.ref_interval

    # New 'gene name' will be <chr>_<interval_start>_<interval_end>
    gene = f"{chrom}_{int(interval_start)}_{int(interval_end)}"
    return gene


def chunks(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i: i + n]


def launch_pool(func, func_args, procs=1):
    """
    Launch pool.

    Use multiprocessing library to create pool and map function calls to
    that pool

    :param procs: Number of processes to use for pool
    :type procs: int, optional
    :param func: Function to exececute in the pool
    :type func: function
    :param func_args: List containing arguments
        for each call to function <funct>
    :type func_args: list
    :return: List of results returned by each call to function <funct>
    :rtype: list
    """
    p = multiprocessing.Pool(processes=procs)
    try:
        results = list(tqdm(p.imap(func, func_args), total=len(func_args)))
        # results = list(p.imap(func, func_args))
        p.close()
        p.join()
    except KeyboardInterrupt:
        p.terminate()
    return results


def run_groupby(df):
    """Run groupby."""
    df["umi_corr"] = df.groupby(["gene_cell"])[
        "umi_uncorr"].transform(correct_umis)
    return df


def process_records(tag_file, args):
    """
    Process bam records.

    Read through all the alignments for specific chromosome to pull out
    the gene, barcode, and uncorrected UMI information. Use that to cluster
    UMIs and get the corrected UMI sequence. We'll then write out a TSV
    file with corrected UMI tag (UB).

    :param tag_file: TSV file path with read_id, CB and UR tags
    :type tag_file: str
    """
    tags = pd.read_csv(tag_file, sep='\t', index_col=0)

    ga_header = ['read_id', 'status', 'mapq', 'gene']
    gene_assigns = pd.read_csv(
        args.gene_assigns, sep='\t', names=ga_header, index_col=0,
        keep_default_na=False)

    try:
        transcript_assigns = pd.read_csv(
            args.transcript_assigns, sep='\t', index_col=0,
            keep_default_na=False)
    except pd.errors.EmptyDataError:
        transcript_assigns = pd.DataFrame()

    df = gene_assigns.merge(
        transcript_assigns,
        left_index=True,
        right_index=True,
    ).merge(
        tags,
        left_index=True,
        right_index=True,
    )

    records = []
    cell_gene_counter = collections.Counter()

    for row in df.itertuples():
        read_id = row.Index

        # Corrected cell barcode = CB:Z
        bc_corr = row.CB

        # Uncorrected UMI = UR:Z
        umi_uncorr = row.UR

        gene = row.gene

        if transcript_assigns is not None:
            transcript = transcript_assigns.loc[read_id, 'ref_id']
        else:
            transcript = '-'

        # If no gene annotation exists
        if gene == "NA":
            # Group by region if no gene annotation
            gene = create_region_name(row, args)

        cell_gene_counter[(bc_corr, gene)] += 1
        if cell_gene_counter[(bc_corr, gene)] <= args.cell_gene_max_reads:
            records.append(
                (read_id, gene, transcript, bc_corr, umi_uncorr))

    # Create a dataframe with chrom-specific data
    df = pd.DataFrame.from_records(
        records, columns=["read_id", "gene", "transcript", "bc", "umi_uncorr"]
    )

    # This is the chunked pandas implementation using multiprocessing module
    df["gene_cell"] = df["gene"] + ":" + df["bc"]
    df = df.set_index("gene_cell")
    gene_cell_unique = list(set(df.index))
    gene_cell_per_chunk = 50
    gene_cell_chunks = chunks(gene_cell_unique, gene_cell_per_chunk)

    func_args = []
    for i, gene_cell_chunk in enumerate(gene_cell_chunks):
        df_ = df.loc[gene_cell_chunk]
        func_args.append(df_)

    results = launch_pool(run_groupby, func_args, args.threads)
    if len(results) > 0:
        df = pd.concat(results, axis=0)
    else:
        df = pd.DataFrame(
            columns=[
                "read_id",
                "gene",
                "bc",
                "umi_uncorr",
                "umi_corr"])

    # Simplify to a read_id:umi_corr dictionary
    df = df.drop(["bc", "umi_uncorr"], axis=1).set_index("read_id")

    # Dict of corrected UMI for each read ID
    umis = df.replace({np.nan:None}).to_dict()["umi_corr"]

    # Dict of gene names to add <chr>_<start>_<end> in place of NA
    genes = df.replace({np.nan:None}).to_dict()["gene"]

    transcripts = df.replace({np.nan:None}).to_dict()["transcript"]

    # Add corrected UMIs to each chrom-specific BAM entry via the UB:Z tag
    read_tags = add_tags(args.chrom, umis, genes, transcripts, args)
    read_tags.to_csv(args.output_read_tags, sep='\t', index=False)


def main(args):
    """Run entry point."""
    init_logger(args)

    tag_file = args.bc_ur_tags
    process_records(tag_file, args)


if __name__ == "__main__":
    args = parse_args()

    main(args)
