#!/bin/bash
cd /Users/ramanambore/projects/ramboq && python3 docs/generate_pdf.py 2>&1 | tail -5
exit_code=$?
if [ $exit_code -eq 0 ]; then
  ls -lh docs/DESIGN_GUIDE.pdf
fi
exit $exit_code
