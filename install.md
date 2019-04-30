OS: linux

This is numpywren lambdapack3.0 branch.
If you want to see other branch, please see https://github.com/Vaishaal/numpywren.

1. Install env:
```
wget https://repo.continuum.io/miniconda/Miniconda3-latest-Linux-x86_64.sh -O miniconda.sh;
export RANDOM_ID=`python -c "from random import choice; print(''.join([choice('1234567890') for i in range(6)]))"`;
bash miniconda.sh -b -p $HOME/miniconda
export PATH="$HOME/miniconda/bin:$PATH"
conda config --set always_yes yes --set changeps1 no
conda update -q conda
conda info -a
conda create -q -n test-environment python=3.6.3 numpy pytest cython nose boto3 PyYAML Click pytest numba scipy
source activate test-environment
pip install glob2 pylint tornado awscli sklearn cloudpickle pywren
```
redis version need 2.10.6:
```
pip install redis==2.10.6
```

If you open a new shell, please redo:
```
export PATH="$HOME/miniconda/bin:$PATH"
source activate test-environment
```

2. setup pywren
please see http://pywren.io/pages/gettingstarted.html to setup pywren.
For lambda_role: I suggest use use `pywren_exec_role_1`, it has permission to access related services.

3. build numpywren
```
python setup.py build
python setup.py install
```
run numpywren
```
numpywren
numpywren setup
```
For error `ModuleNotFoundError: No module named 'numpywren.scripts', do:
```
cp -r numpywren/scripts ~/miniconda/envs/test-environment/lib/python3.6/site-packages/numpywren-0.0.1a0-py3.6.egg/numpywren/
cp -r numpywren/redis_files ~/miniconda/envs/test-environment/lib/python3.6/site-packages/numpywren-0.0.1a0-py3.6.egg/numpywren/
cp -r numpywren/default_config.yaml ~/miniconda/envs/test-environment/lib/python3.6/site-packages/numpywren-0.0.1a0-py3.6.egg/numpywren/
```
```
vim ~/.numpywren_config
```
change `ec2_instance_type: m4.4xlarge` to `ec2_instance_type: m4.large` 

lanuch a ec2 for redis
```
numpywren control-plane launch 
```

Then, you can run:
```
 python tests/test_alg_correctness.py
```
