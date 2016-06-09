#!/usr/bin/python
import argparse, sys, os, gzip, itertools
from shutil import rmtree
from multiprocessing import cpu_count, Pool, Queue, Lock
from tempfile import mkdtemp, gettempdir
from Bio.Format.Sam import BAMIndex, BAMFile
from Bio.Stream import LocusStream
from Bio.Range import ranges_to_coverage, GenomicRange
from subprocess import call

## The purpose of this script is to read through a bam alignment and record as much information as possible from it.  ##
## The bam should be indexed ahead of time in our index format.

gfinished = None
gtotal = None
glock = Lock()

def main():
  #do our inputs
  args = do_inputs()
  bind_path = args.input+'.bgi'
  if not os.path.isfile(bind_path):
    sys.stderr.write("WARNING: index has not been created for:\n"+args.input+"\n")
    sys.stderr.write("We will create an index in a temporary file, but you should make one.\n")
    bind_path = args.tempdir+'/myindex.bgi'
    cmd = "bam_bgzf_index.py "+args.input+" -o "+bind_path
    call(cmd.split())
  sys.stderr.write("Reading index\n")
  bind = BAMIndex(bind_path)
  sys.stderr.write("Checking index for ordering\n")
  bam_ordered = bind.check_ordered()
  if bam_ordered: sys.stderr.write("BAM is ordered\n")
  else: 
    sys.stderr.write("WARNING: BAM is not ordered.  Will not allow some threading advantages.\n")

  # Go through aligned lines via locus stream
  bf = BAMFile(args.input,index_obj=bind)
  chrlens = bf.get_header().get_sequence_lengths()
  global gtotal
  gtotal = len(chrlens.keys())+1
  global gfinished
  gfinished = 0
  of_chrlens = gzip.open(args.tempdir+'/chrlens.txt.gz','w')
  for qname in sorted(chrlens.keys()):
    of_chrlens.write(qname+"\t"+str(chrlens[qname])+"\n")
  of_chrlens.close()
  transcripts = {}
  results = []
  unlens_queue = None
  if args.threads > 1 and bam_ordered:
    p = Pool(processes=args.threads)

  # Go through unaligned lines
  if args.threads > 1 and bam_ordered:
    unlens_queue = p.apply_async(process_unaligned,args=(args,bind,bam_ordered,),callback=count_done)
  else:
    v = process_unaligned(args,bind,bam_ordered)
    unlens_queue = Queue()
    unlens_queue.put(v)
  if args.threads == 1 or not bam_ordered:
    v = process_all(args,bind)
    q = Queue()
    q.put(v)
    results.append(q)
  else:
    # Go through all chromosomes
    for chr in sorted(chrlens.keys()):
      rng = GenomicRange(chr,1,chrlens[chr])
      v = p.apply_async(process_chromosome,args=(args,rng,bind),callback=count_done)
      results.append(v)
  if args.threads > 1 and bam_ordered:
    p.close()
    p.join()
  unlens = unlens_queue.get()
  sys.stderr.write("\n")
  sys.stderr.write("Joining results\n")
  for result in [x.get() for x in results]:
    for qname in result.keys():
      if qname not in transcripts: transcripts[qname] = []
      for data in result[qname]: transcripts[qname].append(data)
  #of_mappability = gzip.open(args.tempdir+'/mappability.txt.gz','w')
  #of_mappability.close()

  # Find Techinical Chimeras

  #for l in unlens:
  #  print l['qname']
  of_chimera = gzip.open(args.tempdir+'/chimera.gpd.gz','w')
  of_gapped = gzip.open(args.tempdir+'/gapped.gpd.gz','w')
  of_technical = gzip.open(args.tempdir+'/technical_chimeras.gpd.gz','w')
  of_technical_atypical = gzip.open(args.tempdir+'/technical_atypical_chimeras.gpd.gz','w')
  of_lengths = gzip.open(args.tempdir+'/lengths.txt.gz','w')
  chimera = 0
  gapped = 0
  technical = 0
  technical_atypical = 0

  best_gpd = []
  for qname in transcripts:
    #print qname
    best_ind = [i for i in range(0,len(transcripts[qname])) if transcripts[qname][i]['flag'] & 2304 == 0][0]
    o_qlen = transcripts[qname][best_ind]['qrng'].length()
    v = check_paths(transcripts[qname],best_ind,args)
    v_qlen = v['qlen']
    if v['type'] == 'chimera':
      chimera += 1
      for p in v['path']:
        of_chimera.write(p['tx'].get_gpd_line()+"\n")
    elif v['type'] == 'self-chimera':
      technical += 1
      for p in v['path']:
        of_technical.write(p['tx'].get_gpd_line()+"\n")
    elif v['type'] == 'self-chimera-atypical':
      technical_atypical += 1
      for p in v['path']:
        of_technical_atypical.write(p['tx'].get_gpd_line()+"\n")
    if v['type'] == 'gapped':
      gapped += 1
      for p in v['path']:
        of_gapped.write(p['tx'].get_gpd_line()+"\n")
    best_gpd.append(transcripts[qname][best_ind]['tx'])
    of_lengths.write(qname+"\t"+v['type']+"\t"+str(o_qlen)+"\t"+str(v_qlen)+"\t"+str(transcripts[qname][best_ind]['qlen'])+"\n")
    #########
    # Only keep the best entry for the transcript
    transcripts[qname] = transcripts[qname][best_ind]
  for r in unlens:
    of_lengths.write(r['qname']+"\tunaligned\t0\t0\t"+str(r['qlen'])+"\n")
  of_chimera.close()
  of_gapped.close()
  of_technical.close()
  of_technical_atypical.close()
  of_lengths.close()

  of_bestgpd = gzip.open(args.tempdir+'/best.sorted.gpd.gz','w')
  for gpd in sorted(best_gpd, key=lambda x: (x.get_range().chr,x.get_range().start,x.get_range().end,x.get_strand())):
    of_bestgpd.write(gpd.get_gpd_line()+"\n")
  of_bestgpd.close()


  # Temporary working directory step 3 of 3 - Cleanup
  if not args.specific_tempdir:
    rmtree(args.tempdir)

# path
# aligned_bases - bases aligned not counting any deltions or insertions
# indecies - 
# type - original/chimera/self-chimera/gapped
# qlen - range spanned by query alignments
def check_paths(path_data,best_ind,args):
  #other_inds = [x for x in range(0,len(path_data)) if x != best_ind]
  possibles = get_index_sets(len(path_data))
  new_best = [path_data[best_ind]]
  new_bases = path_data[best_ind]['aligned_bases']
  new_inds = set([best_ind])
  new_type = 'original'
  new_qlen = path_data[best_ind]['qrng'].length()
  for possible_path in possibles:
    if best_ind not in possible_path: continue # only consider path sets that have our best index in it
    res = evaluate_path(path_data,possible_path,best_ind,args)
    if res['any']:
      bases = sum([x['aligned_bases'] for x in res['path']])
      if bases > new_bases:
        new_best = res['path']
        new_bases = bases
        new_inds = set(possible_path)
        qrngs = [res['path'][0]['qrng']]
        for i in range(1,len(res['path'])):
          if qrngs[-1].overlaps(res['path'][i]['qrng']):
            qrngs[-1] = qrngs[-1].merge(res['path'][i]['qrng'])
          else: qrngs.append(res['path'][i]['qrng'])
        new_qlen = sum([x.length() for x in qrngs])
        if res['gapped']: new_type = 'gapped'
        elif res['chimera']: new_type = 'chimera'
        elif res['self-chimera']: new_type = 'self-chimera'
        elif res['self-chimera-atypical']: new_type = 'self-chimera-atypical'
        else:
          sys.stderr.write("WARNING: Unaccounted for type\n")
  return {'path':new_best, 'aligned_bases':new_bases, 'indecies':new_inds,'type':new_type,'qlen':new_qlen}
  #print path_data[best_ind]

# Create a dictionary with the follwing information
# path: a list of alignments order by query placement
# gapped: is it a gapped alignment
# chimera: is it a fusion-like 
# self-chimera: is it a + - of an overlapping target sequence
def evaluate_path(path_data,possible_path,best_ind,args):
  pord = sorted([path_data[i] for i in possible_path],key=lambda x: x['qrng'].start)
  best_bases = path_data[best_ind]['aligned_bases']
  bases = sum([x['aligned_bases'] for x in pord])
  res = {'path':pord,'gapped':False,'chimera':False,'self-chimera':False,'self-chimera-atypical':False,'any':False}
  if len(path_data) <= 1: return res
  if bases+bases*args.required_fractional_improvement < best_bases:  
    return res
  for p in pord:
    if p['aligned_bases'] < args.min_aligned_bases: return res
  # check for query overlaps ... not a useful build
  for i in range(0,len(pord)):
    for j in range(i+1,len(pord)):
      if args.max_query_gap:
        if pord[i]['qrng'].distance(pord[j]['qrng']) > args.max_query_gap: return res
      if pord[i]['qrng'].overlap_size(pord[j]['qrng']) > args.max_query_overlap:
        return res

  chrcount = len(set([x['tx'].get_range().chr for x in pord]))

  # check for target overlaps ... not gapped or chimera but maybe self-chimera
  for i in range(0,len(pord)):
    for j in range(i+1,len(pord)):  
      if pord[i]['tx'].overlap_size(pord[j]['tx']) > args.max_target_overlap:
        #res['gapped'] = False
        #res['chimera'] = False
        if pord[i]['tx'].get_strand() != pord[j]['tx'].get_strand() and chrcount == 1:
          res['self-chimera'] = True
          res['any'] = True
        else: 
          res['self-chimera-atypical'] = True
          res['any'] = True
        return res

  for i in range(0,len(pord)):
    for j in range(i+1,len(pord)):  
      if args.max_target_gap:
        dist = pord[i]['tx'].get_range().distance(pord[j]['tx'].get_range())
        if dist > args.max_target_gap or dist == -1: 
          res['chimera'] = True
          res['gapped'] = False
          res['any'] = True
  if len(pord) > 1 and not res['self-chimera'] and not res['chimera']:
    res['gapped'] = True
    res['any'] = True
  return res

def process_unaligned(args,bind,bam_ordered):
  if args.threads == 1: sys.stderr.write("Reading unaligned reads\n")
  buncoord = bind.get_unaligned_start_coord()
  bf = BAMFile(args.input,index_obj=bind)
  if bam_ordered:
    bfun = bf.fetch_starting_at_coord(buncoord)
  else:
    bfun = bf
  unlens = []
  for eun in bfun:
    if eun.is_aligned(): continue
    unlens.append({'qname':eun.value('qname'),'qlen':eun.get_query_length()})
  if args.threads == 1: sys.stderr.write("Finished reading unaligned reads\n")
  return unlens

def count_done(v):
  global glock
  global gtotal
  global gfinished
  glock.acquire()
  gfinished += 1
  sys.stderr.write("Finished "+str(gfinished)+"/"+str(gtotal)+"    \r")
  glock.release()
  return v

def process_all(args,bind):
  bf = BAMFile(args.input,index_obj=bind)
  transcripts = {}
  z = 0
  tot = bind.get_length()
  for e in bf:
    z += 1
    if z%100==0:
      sys.stderr.write("Alignments processed: "+str(z)+"/"+str(tot)+"           \r")
    if not e.is_aligned(): continue
    tx = e.get_target_transcript(68)
    qname = e.value('qname')
    if qname not in transcripts:  transcripts[qname] = []
    bil = bind.get_index_line(e.get_line_number())
    if bil['qname'] != qname: 
      sys.stderr.write("ERROR: problem matching line to index. perhaps index was not sorted\n")
      sys.exit()
    #only save parts we will be using to reduce memory footprint
    transcripts[qname].append({'qrng':e.get_actual_original_query_range(),'tx':e.get_target_transcript(68),'flag':bil['flag'],'qlen':e.get_query_length(),'aligned_bases':e.get_aligned_bases_count()})
  return transcripts
  

def process_chromosome(args,rng,bind):
  bf = BAMFile(args.input,index_obj=bind)
  sub_bf = bf.fetch_by_range(rng)
  transcripts = {}
  for e in sub_bf:
      if args.threads == 1:
        sys.stderr.write("Alignment Range: "+e.get_target_range().get_range_string()+"           \r")
      tx = e.get_target_transcript(68)
      qname = e.value('qname')
      #tx.get_transcript_name() 
      if qname not in transcripts:  transcripts[qname] = []
      bil = bind.get_index_line(e.get_line_number())
      if bil['qname'] != qname: 
        sys.stderr.write("ERROR: problem matching line to index. perhaps index was not sorted\n")
        sys.exit()
      #only save parts we will be using to reduce memory footprint
      transcripts[qname].append({'qrng':e.get_actual_original_query_range(),'tx':e.get_target_transcript(68),'flag':bil['flag'],'qlen':e.get_query_length(),'aligned_bases':e.get_aligned_bases_count()})
  return transcripts
        

def get_index_sets(indlen):
  r = []
  inds = range(0,indlen)
  for l in range(1,len(inds)+1):
    for subset in itertools.combinations(inds,l):
      r.append(subset)
  return r

# given the alignments return the compatible paths
def get_compatible_paths(alns,args):
  return

def do_inputs():
  # Setup command line inputs
  parser=argparse.ArgumentParser(description="",formatter_class=argparse.ArgumentDefaultsHelpFormatter)
  parser.add_argument('input',help="BAMFILE input")
  parser.add_argument('-o','--output',help="OUTPUTFILE or STDOUT if not set")
  parser.add_argument('--threads',type=int,default=cpu_count(),help="INT number of threads to run. Default is system cpu count")

  # Arguments for finding alternate multi alignment paths
  parser.add_argument('--min_aligned_bases',type=int,default=50,help="Don't consider very short alignments")
  parser.add_argument('--max_query_overlap',type=int,default=10,help="Consider two alignments incompatible if greater overlap than this")
  parser.add_argument('--max_target_overlap',type=int,default=10,help="Consider two alignments incompatible if greater overlap than this")
  parser.add_argument('--max_target_gap',type=int,default=500000,help="Not a gapped alignment if gap is greater than this")
  parser.add_argument('--max_query_gap',type=int,default=500000,help="Consider a gapped alignment incompatible if greater thant this")
  parser.add_argument('--required_fractional_improvement',type=float,default=0.2,help="combination path should be this much better than best single alignment")  

  # Temporary working directory step 1 of 3 - Definition
  group = parser.add_mutually_exclusive_group()
  group.add_argument('--tempdir',default=gettempdir(),help="The temporary directory is made and destroyed here.")
  group.add_argument('--specific_tempdir',help="This temporary directory will be used, but will remain after executing.")
  args = parser.parse_args()

  # Temporary working directory step 2 of 3 - Creation
  setup_tempdir(args)
  return args

def setup_tempdir(args):
  if args.specific_tempdir:
    if not os.path.exists(args.specific_tempdir):
      os.makedirs(args.specific_tempdir.rstrip('/'))
    args.tempdir = args.specific_tempdir.rstrip('/')
    if not os.path.exists(args.specific_tempdir.rstrip('/')):
      sys.stderr.write("ERROR: Problem creating temporary directory\n")
      sys.exit()
  else:
    args.tempdir = mkdtemp(prefix="weirathe.",dir=args.tempdir.rstrip('/'))
    if not os.path.exists(args.tempdir.rstrip('/')):
      sys.stderr.write("ERROR: Problem creating temporary directory\n")
      sys.exit()
  if not os.path.exists(args.tempdir):
    sys.stderr.write("ERROR: Problem creating temporary directory\n")
    sys.exit()
  return 

if __name__=="__main__":
  main()