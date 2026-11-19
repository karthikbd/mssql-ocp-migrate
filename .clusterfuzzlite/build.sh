#!/bin/bash -eu
# Builds the Atheris fuzzers in tests/fuzz for ClusterFuzzLite.
pip3 install --no-deps .
for fuzzer in $(find "$SRC/mssql-ocp-migrate/tests/fuzz" -name 'fuzz_*.py'); do
  compile_python_fuzzer "$fuzzer"
done
