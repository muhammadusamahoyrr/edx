#!/usr/bin/env bash
SRC="/mnt/c/Users/The Laptop Hut/Desktop/edx/coursemate/eval"
rm -rf /home/dev/cm-eval
mkdir -p /home/dev/cm-eval
cp -r "$SRC/datasets" "$SRC/harness" "$SRC/run_eval.py" /home/dev/cm-eval/
find /home/dev/cm-eval -type f \( -name '*.py' -o -name '*.yaml' \) -exec sed -i 's/\r$//' {} \;
docker exec -u root tutor_local-coursemate-1 rm -rf /eval
docker cp /home/dev/cm-eval tutor_local-coursemate-1:/eval
docker exec -u root tutor_local-coursemate-1 chown -R coursemate:coursemate /eval
echo "staged:"
docker exec tutor_local-coursemate-1 ls /eval
