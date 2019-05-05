import time
import random
from timeit import default_timer as timer
import string
import concurrent.futures as fs

from numpywren import compiler, job_runner, kernels
from numpywren.matrix import BigMatrix
from numpywren.alg_wrappers import cholesky, tsqr, gemm, qr, bdfac
import numpy as np
from numpywren.matrix_init import shard_matrix
from numpywren.algs import *
from numpywren.compiler import lpcompile, walk_program, find_parents, find_children
from numpywren import config
import pywren
import pywren.wrenconfig as wc

def run_program_in_pywren(program, num_workers=32):
    def pywren_run(_):
        job_runner.lambdapack_run(program, timeout=60, idle_timeout=6)
    default_npw_config = config.default()
    pywren_config = wc.default()
    npw_config = config.default()
    pywren_config['runtime']['s3_bucket'] = npw_config['runtime']['bucket']
    pywren_config['runtime']['s3_key'] = npw_config['runtime']['s3_key']
    pwex = pywren.default_executor(config=pywren_config)
    futures = pwex.map(pywren_run, range(num_workers))
    return futures


def test_cholesky():
    X = np.random.randn(64, 64)
    A = X.dot(X.T) + np.eye(X.shape[0])
    #shard_size = 8
    shard_size = 16
    shard_sizes = (shard_size, shard_size)
    A_sharded= BigMatrix("cholesky_test_A", shape=A.shape, shard_sizes=shard_sizes, write_header=True)
    A_sharded.free()
    shard_matrix(A_sharded, A)
    program, meta =  cholesky(A_sharded)
    executor = fs.ProcessPoolExecutor(1)
    print("starting program")
    program.start()
    future = executor.submit(job_runner.lambdapack_run, program, timeout=60*20, idle_timeout=6)
    program.wait()
    program.free()
    L_sharded = meta["outputs"][0]
    L_npw = L_sharded.numpy()
    L = np.linalg.cholesky(A)
    assert(np.allclose(L_npw, L))
    print("great success!")

def test_cholesky_lambda():
    X = np.random.randn(64, 64)
    A = X.dot(X.T) + np.eye(X.shape[0])
    shard_size = 16
    shard_sizes = (shard_size, shard_size)
    A_sharded= BigMatrix("cholesky_test_A", shape=A.shape, shard_sizes=shard_sizes, write_header=True)
    A_sharded.free()
    shard_matrix(A_sharded, A)
    program, meta =  cholesky(A_sharded)
    futures = run_program_in_pywren(program)
    program.start()
    program.wait()
    program.free()
    L_sharded = meta["outputs"][0]
    L_npw = L_sharded.numpy()
    L = np.linalg.cholesky(A)
    assert(np.allclose(L_npw, L))
    print("great success!")


def test_tsqr():
    np.random.seed(1)
    size = 256
    shard_size = 32
    X = np.random.randn(size, shard_size)
    Q,R = np.linalg.qr(X)
    q0, r0 = np.linalg.qr(X[:2,:2])
    q1, r1 = np.linalg.qr(X[2:,:2])
    r2 = np.linalg.qr(np.vstack((r0,r1)))[1]
    shard_sizes = (shard_size, X.shape[1])
    X_sharded = BigMatrix("tsqr_test_X", shape=X.shape, shard_sizes=shard_sizes, write_header=True)
    shard_matrix(X_sharded, X)
    program, meta = tsqr(X_sharded)
    executor = fs.ProcessPoolExecutor(1)
    print("starting program")
    program.start()
    future = executor.submit(job_runner.lambdapack_run, program, timeout=10*120, idle_timeout=6)
    program.wait()
    program.free()
    R_sharded = meta["outputs"][0]
    num_tree_levels = int(np.log(np.ceil(size/shard_size))/np.log(2))
    print("num_tree_levels", num_tree_levels)
    R_npw = R_sharded.get_block(max(num_tree_levels, 0), 0)
    sign_matrix_local = np.eye(R.shape[0])
    sign_matrix_remote = np.eye(R.shape[0])
    sign_matrix_local[np.where(np.diag(R) <= 0)]  *= -1
    sign_matrix_remote[np.where(np.diag(R_npw) <= 0)]  *= -1
    # make the signs match
    R_npw *= np.diag(sign_matrix_remote)[:, np.newaxis]
    R  *= np.diag(sign_matrix_local)[:, np.newaxis]
    assert(np.allclose(R_npw, R))
    print("great success!")

def test_tsqr_lambda():
    np.random.seed(1)
    size = 256
    shard_size = 32
    X = np.random.randn(size, shard_size)
    Q,R = np.linalg.qr(X)
    q0, r0 = np.linalg.qr(X[:2,:2])
    q1, r1 = np.linalg.qr(X[2:,:2])
    r2 = np.linalg.qr(np.vstack((r0,r1)))[1]
    shard_sizes = (shard_size, X.shape[1])
    X_sharded = BigMatrix("tsqr_test_X", shape=X.shape, shard_sizes=shard_sizes, write_header=True)
    shard_matrix(X_sharded, X)
    program, meta = tsqr(X_sharded)
    executor = fs.ProcessPoolExecutor(1)
    print("starting program")
    program.start()
    futures = run_program_in_pywren(program)
    program.wait()
    program.free()
    R_sharded = meta["outputs"][0]
    num_tree_levels = int(np.log(np.ceil(size/shard_size))/np.log(2))
    print("num_tree_levels", num_tree_levels)
    R_npw = R_sharded.get_block(max(num_tree_levels, 0), 0)
    sign_matrix_local = np.eye(R.shape[0])
    sign_matrix_remote = np.eye(R.shape[0])
    sign_matrix_local[np.where(np.diag(R) <= 0)]  *= -1
    sign_matrix_remote[np.where(np.diag(R_npw) <= 0)]  *= -1
    # make the signs match
    R_npw *= np.diag(sign_matrix_remote)[:, np.newaxis]
    R  *= np.diag(sign_matrix_local)[:, np.newaxis]
    assert(np.allclose(R_npw, R))

#@profile
def test_gemm():
    size = 64
    #np.random.seed(0)
    A = np.random.randn(size, size)
    B = np.random.randn(size, size)
    C = np.dot(A, B)
    shard_sizes = (16,16)
    A_sharded = BigMatrix("Gemm_test_A", shape=A.shape, shard_sizes=shard_sizes, write_header=True)
    B_sharded = BigMatrix("Gemm_test_B", shape=A.shape, shard_sizes=shard_sizes, write_header=True)
    shard_matrix(A_sharded, A)
    shard_matrix(B_sharded, B)
    program, meta = gemm(A_sharded, B_sharded)
    program.start()
    job_runner.lambdapack_run(program, timeout=60*20, idle_timeout=6, pipeline_width=3)
    program.wait()
    program.free()
    C_sharded = meta["outputs"][0]
    C_npw = C_sharded.numpy()
    assert(np.allclose(C_npw, C))
    return

def test_gemm_lambda():
    size = 32
    A = np.random.randn(size, size)
    B = np.random.randn(size, size)
    C = np.dot(A, B)
    shard_sizes = (8,8)
    A_sharded = BigMatrix("Gemm_test_A", shape=A.shape, shard_sizes=shard_sizes, write_header=True)
    B_sharded = BigMatrix("Gemm_test_B", shape=A.shape, shard_sizes=shard_sizes, write_header=True)
    shard_matrix(A_sharded, A)
    shard_matrix(B_sharded, B)
    program, meta = gemm(A_sharded, B_sharded)
    executor = fs.ProcessPoolExecutor(1)
    program.start()
    run_program_in_pywren(program)
    program.wait()
    program.free()
    C_sharded = meta["outputs"][0]
    C_npw = C_sharded.numpy()
    assert(np.allclose(C_npw, C))
    return

def test_qr():
    N = 28
    shard_size = 7
    shard_sizes = (shard_size, shard_size)
    X = np.random.randn(N, N)
    X_sharded = BigMatrix("QR_input_X", shape=X.shape, shard_sizes=shard_sizes, write_header=True)
    N_blocks = X_sharded.num_blocks(0)
    shard_matrix(X_sharded, X)
    program, meta = qr(X_sharded)
    executor = fs.ProcessPoolExecutor(2)
    program.start()
    print("starting program...")
    future = executor.submit(job_runner.lambdapack_run, program, timeout=60*10, idle_timeout=6, pipeline_width=1)
    future = executor.submit(job_runner.lambdapack_run, program, timeout=60*10, idle_timeout=6, pipeline_width=1)
    future = executor.submit(job_runner.lambdapack_run, program, timeout=60*10, idle_timeout=6, pipeline_width=1)
    program.wait()
    program.free()
    Rs = meta["outputs"][0]
    R_remote = Rs.get_block(N_blocks - 1, N_blocks - 1, 0)
    R_local = np.linalg.qr(X)[1][-shard_size:, -shard_size:]
    sign_matrix_local = np.eye(R_local.shape[0])
    sign_matrix_remote = np.eye(R_local.shape[0])
    sign_matrix_local[np.where(np.diag(R_local) <= 0)]  *= -1
    sign_matrix_remote[np.where(np.diag(R_remote) <= 0)]  *= -1
    # make the signs match
    R_remote *= np.diag(sign_matrix_remote)[:, np.newaxis]
    R_local  *= np.diag(sign_matrix_local)[:, np.newaxis]
    assert(np.allclose(R_local, R_remote))
def bdfac_python(x, block_size=2):
    N = int(x.shape[0]/block_size)
    U = np.eye(x.shape[0])
    V = np.eye(x.shape[0])
    S = x.copy()
    bs = block_size
    for i in range(N):
        # local QR decomposition
        u_part,s = np.linalg.qr(S[i*bs:, i*bs:(i+1)*bs], mode="complete")
        u_full = np.eye(x.shape[0])
        u_full[i*bs:, i*bs:] = u_part
        U = U.dot(u_full)
        S[i*bs:, i*bs:(i+1)*bs] = s
        if (i+1)*bs < x.shape[0]:
            S[i*bs:,(i+1)*bs:] = u_part.T.dot(S[i*bs:,(i+1)*bs:])
            print(S[i*bs:(i+1)*bs, (i+1)*bs:].shape)
            v_part,s = np.linalg.qr(S[i*bs:(i+1)*bs, (i+1)*bs:].T, mode="complete")
            v_full = np.eye(x.shape[0])
            v_full[(i+1)*bs:, (i+1)*bs:] = v_part
            V = V.dot(v_full)
            S[i*bs:(i+1)*bs, (i+1)*bs:] = s.T
            if (i+2)*bs <= x.shape[0]:
                print("trailing update")
                print(v_full.shape)
                S[(i+1)*bs:,(i+1)*bs:] = S[(i+1)*bs:,(i+1)*bs:].dot(v_part)
    return U,S,V

#@profile
def test_bdfac():
    N = 16
    shard_size = 4
    shard_sizes = (shard_size, shard_size)
    np.random.seed(0)
    X = np.random.randn(N, N)
    U,S,V = bdfac_python(X, block_size=shard_size)
    svd_bdfac = np.linalg.svd(S, compute_uv=False)
    svd_local = np.linalg.svd(X, compute_uv=False)
    print(svd_bdfac)
    print(svd_local)
    assert(np.allclose(svd_bdfac, svd_local))
    X_sharded = BigMatrix("BDFAC_input_X", shape=X.shape, shard_sizes=shard_sizes, write_header=True)
    N_blocks = X_sharded.num_blocks(0)
    shard_matrix(X_sharded, X)
    program, meta = bdfac(X_sharded)
    executor = fs.ProcessPoolExecutor(1)
    program.start()
    job_runner.lambdapack_run(program, timeout=200, idle_timeout=200, pipeline_width=1)
    program.wait()
    print("returned..")
    program.free()
    R = meta["outputs"][1]
    L = meta["outputs"][0]
    print("====="*10)
    R_remote =  R.get_block(N_blocks -1, 0, N_blocks - 1)
    R_local = S[-shard_size:, -shard_size:]
    print("original", R_local)
    print("remote", R_remote)
    print('==='*10)
    sign_matrix_local = np.eye(R_local.shape[0])
    sign_matrix_remote = np.eye(R_local.shape[0])
    sign_matrix_local[np.where(np.diag(R_local) <= 0)]  *= -1
    sign_matrix_remote[np.where(np.diag(R_remote) <= 0)]  *= -1
    # make the signs match
    R_remote *= np.diag(sign_matrix_remote)[:, np.newaxis]
    R_local  *= np.diag(sign_matrix_local)[:, np.newaxis]
    print(R_local)
    print(R_remote)
    assert(np.allclose(np.abs(R_local), np.abs(R_remote)))
    fac = np.block([[R.get_block(0, 2, 0), L.get_block(0, 2, 1), np.zeros(shard_sizes), np.zeros(shard_sizes)],
                    [np.zeros(shard_sizes), R.get_block(1, 2, 1), L.get_block(1, 1, 2), np.zeros(shard_sizes)],
                    [np.zeros(shard_sizes), np.zeros(shard_sizes), R.get_block(2, 1, 2), L.get_block(2, 0, 3)],
                    [np.zeros(shard_sizes), np.zeros(shard_sizes), np.zeros(shard_sizes), R.get_block(3, 0, 3)]])

    svd_remote = np.linalg.svd(fac, compute_uv=False)
    svd_local = np.linalg.svd(X, compute_uv=False)
    assert(np.allclose(svd_remote, svd_local))
    return 0



def test_bdfac_truncated():
    N = 16
    shard_size = 4
    shard_sizes = (shard_size, shard_size)
    np.random.seed(0)
    X = np.random.randn(N, N)
    U,S,V = bdfac_python(X, block_size=shard_size)
    svd_bdfac = np.linalg.svd(S, compute_uv=False)
    svd_local = np.linalg.svd(X, compute_uv=False)
    print(svd_bdfac)
    print(svd_local)
    assert(np.allclose(svd_bdfac, svd_local))
    X_sharded = BigMatrix("BDFAC_input_X", shape=X.shape, shard_sizes=shard_sizes, write_header=True)
    N_blocks = X_sharded.num_blocks(0)
    shard_matrix(X_sharded, X)
    program, meta = bdfac(X_sharded, truncate=2)
    executor = fs.ProcessPoolExecutor(1)
    program.start()
    executor.submit(job_runner.lambdapack_run, program, timeout=200*50, idle_timeout=200, pipeline_width=1)
    program.wait()
    print("returned..")



def test_qr_lambda():
    N = 16
    shard_size = 8
    shard_sizes = (shard_size, shard_size)
    X = np.random.randn(N, N)
    X_sharded = BigMatrix("QR_input_X", shape=X.shape, shard_sizes=shard_sizes, write_header=True)
    N_blocks = X_sharded.num_blocks(0)
    shard_matrix(X_sharded, X)
    program, meta = qr(X_sharded)
    print(program.hash)
    program.start()
    print("starting program...")
    futures = run_program_in_pywren(program, num_workers=1)
    #futures[0].result()
    program.wait()
    program.free()
    Rs = meta["outputs"][0]
    R_remote = Rs.get_block(N_blocks - 1, N_blocks - 1, 0)
    R_local = np.linalg.qr(X)[1][-shard_size:, -shard_size:]
    sign_matrix_local = np.eye(R_local.shape[0])
    sign_matrix_remote = np.eye(R_local.shape[0])
    sign_matrix_local[np.where(np.diag(R_local) <= 0)]  *= -1
    sign_matrix_remote[np.where(np.diag(R_remote) <= 0)]  *= -1
    # make the signs match
    R_remote *= np.diag(sign_matrix_remote)[:, np.newaxis]
    R_local  *= np.diag(sign_matrix_local)[:, np.newaxis]
    assert(np.allclose(R_local, R_remote))









if __name__ == "__main__":
    #test_cholesky()
    #test_tsqr()
    #test_qr()
    #test_cholesky_lambda()
    #test_tsqr_lambda()
    #test_gemm_lambda()
    #test_qr_lambda()
    #test_gemm()
    test_bdfac_truncated()
    #test_bdfac()
    pass

