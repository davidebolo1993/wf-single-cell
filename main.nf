#!/usr/bin/env nextflow

// Developer notes
//
// This template workflow provides a basic structure to copy in order
// to create a new workflow. Current recommended pratices are:
//     i) create a simple command-line interface.
//    ii) include an abstract workflow scope named "pipeline" to be used
//        in a module fashion
//   iii) a second concreate, but anonymous, workflow scope to be used
//        as an entry point when using this workflow in isolation.

import groovy.json.JsonBuilder
nextflow.enable.dsl = 2

include { fastq_ingress } from './lib/fastqingress'
include { stranding } from './subworkflows/stranding'
include { align } from './subworkflows/align'
include { process_bams } from './subworkflows/process_bams'


process summariseAndCatReads {
    // concatenate fastq and fastq.gz in a dir

    label "wfsockeye"
    cpus 1
    input:
        tuple path(directory), val(meta)
    output:
        tuple val("${meta.sample_id}"), path("${meta.sample_id}.stats"), emit: stats
        tuple val("${meta.sample_id}"), path("${meta.sample_id}.fastq"), emit: fastq
    shell:
    """
    fastcat -s ${meta.sample_id} -r ${meta.sample_id}.stats -x ${directory} > ${sample_id}.fastq
    """
}


process getVersions {
    label "wfsockeye"
    cpus 1
    output:
        path "versions.txt"
    script:
    """
    python -c "import pysam; print(f'pysam,{pysam.__version__}')" >> versions.txt
    fastcat --version | sed 's/^/fastcat,/' >> versions.txt
    """
}


process getParams {
    label "wfsockeye"
    cpus 1
    output:
        path "params.json"
    script:
        def paramsJSON = new JsonBuilder(params).toPrettyString()
    """
    # Output nextflow params object to JSON
    echo '$paramsJSON' > params.json
    """
}



// See https://github.com/nextflow-io/nextflow/issues/1636
// This is the only way to publish files from a workflow whilst
// decoupling the publish from the process steps.
process output {
    // publish inputs to output directory
    publishDir "${params.out_dir}", mode: 'copy', pattern: "*", 
        saveAs: { filename -> "${sample_id}/$filename" }
    label "isoforms"
    input:
        tuple val(sample_id),
              path(fname)
    output:
        path fname
    """
    echo "Writing output files"
    """
}


// workflow module
workflow pipeline {
    take:
        sc_sample_sheet
        REF_GENOME_DIR
    main:
        inputs = Channel.fromPath(sc_sample_sheet)
                    .splitCsv(header:true)
                    .map { row -> tuple(
                              row.run_id, 
                              row.kit_name, 
                              row.kit_version, 
                              file(row.path))}
        
        // Sockeye
        stranding(
            inputs,
            sc_sample_sheet)
        
        // 10x reference downloads have known names
        REF_GENOME_FASTA = file("${REF_GENOME_DIR}/fasta/genome.fa", checkIfExists: true)
        REF_GENES_GTF = file("${REF_GENOME_DIR}/genes/genes.gtf", checkIfExists: true)
        ref_genome_idx = file("${REF_GENOME_FASTA}.fai", checkIfExists: true)
        
        if (params.kit_config){
            kit_configs = file("${params.kit_config}/kit_configs.csv", checkIfExists: true)
        }else{
            kit_configs = file("${projectDir}/kit_configs.csv", checkIfExists: true)
        }
        
        align(
            stranding.out.STRANDED_FQ,
            REF_GENOME_FASTA,
            REF_GENES_GTF,
            ref_genome_idx
        )

        process_bams(
            align.out.BAM_SORT,
            align.out.BAM_SORT_BAI,
            sc_sample_sheet,
            kit_configs,
            REF_GENES_GTF
        )
        results = process_bams.out.results
    emit:
        results

}


// entrypoint workflow
WorkflowMain.initialise(workflow, params, log)
workflow {
    sc_sample_sheet = file(params.single_cell_sample_sheet, checkIfExists: true)
    REF_GENOME_DIR = file(params.REF_GENOME_DIR, checkIfExists: true)

    pipeline(sc_sample_sheet, REF_GENOME_DIR)
    
    output(pipeline.out.results.flatMap({it ->
        l = [];
            for (i=1; i<it.size(); i++) {
                l.add(tuple(it[0], it[i]))
            }
            return l


        }).view()
    )
}
