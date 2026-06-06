#!/usr/bin/env python3
"""Bulk-record ICP prechecks for one dm in a single process.
Usage: bulk_icp.py DM_ID 'project=label:notes' 'project2=label2:notes2' ...
label in {icp_match,icp_miss,disqualified,unknown}
"""
import sys, subprocess
dm_id=sys.argv[1]
for arg in sys.argv[2:]:
    proj, rest = arg.split('=',1)
    if ':' in rest:
        label, notes = rest.split(':',1)
    else:
        label, notes = rest, ''
    cmd=['python3','scripts/dm_conversation.py','set-icp-precheck','--dm-id',dm_id,
         '--project',proj,'--label',label]
    if notes: cmd += ['--notes',notes]
    r=subprocess.run(cmd,capture_output=True,text=True)
    print(proj,label,'->',('ok' if r.returncode==0 else 'ERR '+r.stderr.strip()[:80]))
