#!/usr/bin/env python
# Copyright 2014-2021 The PySCF Developers. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import warnings
import ctypes
import numpy as np
import scipy
import scipy.linalg
from pyscf import lib
from pyscf.lib import logger
from pyscf.gto import ATM_SLOTS, BAS_SLOTS, ATOM_OF, PTR_COORD
from pyscf.pbc.lib.kpts_helper import get_kconserv, get_kconserv3  # noqa
from pyscf.pbc.lib.kpts_helper import intersection
from pyscf import __config__

FFT_ENGINE = getattr(__config__, 'pbc_tools_pbc_fft_engine', 'NUMPY+BLAS')

def _fftn_blas(f, mesh):
    assert f.ndim == 4
    mx, my, mz = mesh
    expRGx = np.exp(-2j*np.pi*np.arange(mx)[:,None] * np.fft.fftfreq(mx))
    expRGy = np.exp(-2j*np.pi*np.arange(my)[:,None] * np.fft.fftfreq(my))
    expRGz = np.exp(-2j*np.pi*np.arange(mz)[:,None] * np.fft.fftfreq(mz))
    blksize = max(int(1e5 / (mx * my * mz)), 8) * 4
    n = f.shape[0]
    out = np.empty((n, mx*my*mz), dtype=np.complex128)
    buf = np.empty((blksize, mx*my*mz), dtype=np.complex128)
    for i0, i1 in lib.prange(0, n, blksize):
        ni = i1 - i0
        buf1 = buf[:ni]
        out1 = out[i0:i1]
        g = lib.transpose(f[i0:i1].reshape(ni,-1), out=buf1.reshape(-1,ni))
        g = lib.dot(g.reshape(mx,-1).T, expRGx, c=out1.reshape(-1,mx))
        g = lib.dot(g.reshape(my,-1).T, expRGy, c=buf1.reshape(-1,my))
        g = lib.dot(g.reshape(mz,-1).T, expRGz, c=out1.reshape(-1,mz))
    return out.reshape(n, *mesh)

def _ifftn_blas(g, mesh):
    assert g.ndim == 4
    mx, my, mz = mesh
    expRGx = np.exp(2j*np.pi*np.fft.fftfreq(mx)[:,None] * np.arange(mx))
    expRGy = np.exp(2j*np.pi*np.fft.fftfreq(my)[:,None] * np.arange(my))
    expRGz = np.exp(2j*np.pi*np.fft.fftfreq(mz)[:,None] * np.arange(mz))
    blksize = max(int(1e5 / (mx * my * mz)), 8) * 4
    n = g.shape[0]
    out = np.empty((n, mx*my*mz), dtype=np.complex128)
    buf = np.empty((blksize, mx*my*mz), dtype=np.complex128)
    for i0, i1 in lib.prange(0, n, blksize):
        ni = i1 - i0
        buf1 = buf[:ni]
        out1 = out[i0:i1]
        f = lib.transpose(g[i0:i1].reshape(ni,-1), out=buf1.reshape(-1,ni))
        f = lib.dot(f.reshape(mx,-1).T, expRGx, 1./mx, c=out1.reshape(-1,mx))
        f = lib.dot(f.reshape(my,-1).T, expRGy, 1./my, c=buf1.reshape(-1,my))
        f = lib.dot(f.reshape(mz,-1).T, expRGz, 1./mz, c=out1.reshape(-1,mz))
    return out.reshape(n, *mesh)

nproc = lib.num_threads()

def _fftn_wrapper(a):  # noqa
    return scipy.fft.fftn(a, axes=(1,2,3), workers=nproc)

def _ifftn_wrapper(a):  # noqa
    return scipy.fft.ifftn(a, axes=(1,2,3), workers=nproc)

if FFT_ENGINE == 'FFTW':
    try:
        libfft = lib.load_library('libfft')
    except OSError:
        raise RuntimeError("Failed to load libfft")

    def _copy_d2z(a):
        fn = libfft._copy_d2z
        out = np.empty(a.shape, dtype=np.complex128)
        fn(out.ctypes.data_as(ctypes.c_void_p),
           a.ctypes.data_as(ctypes.c_void_p),
           ctypes.c_size_t(a.size))
        return out

    def _complex_fftn_fftw(f, mesh, func):
        if f.dtype == np.double and f.flags.c_contiguous:
            # np.asarray or np.astype is too slow
            f = _copy_d2z(f)
        else:
            f = np.asarray(f, order='C', dtype=np.complex128)
        mesh = np.asarray(mesh, order='C', dtype=np.int32)
        rank = len(mesh)
        out = np.empty_like(f)
        fn = getattr(libfft, func)
        for i, fi in enumerate(f):
            fn(fi.ctypes.data_as(ctypes.c_void_p),
               out[i].ctypes.data_as(ctypes.c_void_p),
               mesh.ctypes.data_as(ctypes.c_void_p),
               ctypes.c_int(rank))
        return out

    def _fftn_wrapper(a):  # noqa
        mesh = a.shape[1:]
        return _complex_fftn_fftw(a, mesh, 'fft')
    def _ifftn_wrapper(a):  # noqa
        mesh = a.shape[1:]
        return _complex_fftn_fftw(a, mesh, 'ifft')

elif FFT_ENGINE == 'PYFFTW':
    # Note: pyfftw is likely slower than scipy.fft in multi-threading environments
    try:
        import pyfftw
        pyfftw.config.PLANNER_EFFORT = 'FFTW_MEASURE'
        pyfftw.interfaces.cache.enable()
        def _fftn_wrapper(a):  # noqa
            return pyfftw.interfaces.numpy_fft.fftn(a, axes=(1,2,3), threads=nproc)
        def _ifftn_wrapper(a):  # noqa
            return pyfftw.interfaces.numpy_fft.ifftn(a, axes=(1,2,3), threads=nproc)
    except ImportError:
        print('PyFFTW not installed. SciPy fft module will be used.')

elif FFT_ENGINE == 'NUMPY+BLAS':
    _EXCLUDE = [17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71, 73, 79,
                83, 89, 97,101,103,107,109,113,127,131,137,139,149,151,157,163,
                167,173,179,181,191,193,197,199,211,223,227,229,233,239,241,251,
                257,263,269,271,277,281,283,293]
    _EXCLUDE = set(_EXCLUDE + [n*2 for n in _EXCLUDE[:30]] + [n*3 for n in _EXCLUDE[:20]])
    def _fftn_wrapper(a):  # noqa
        mesh = a.shape[1:]
        if mesh[0] in _EXCLUDE and mesh[1] in _EXCLUDE and mesh[2] in _EXCLUDE:
            return _fftn_blas(a, mesh)
        else:
            return scipy.fft.fftn(a, axes=(1,2,3), workers=nproc)
    def _ifftn_wrapper(a):  # noqa
        mesh = a.shape[1:]
        if mesh[0] in _EXCLUDE and mesh[1] in _EXCLUDE and mesh[2] in _EXCLUDE:
            return _ifftn_blas(a, mesh)
        else:
            return scipy.fft.ifftn(a, axes=(1,2,3), workers=nproc)

elif FFT_ENGINE == 'BLAS':
    def _fftn_wrapper(a):  # noqa
        mesh = a.shape[1:]
        return _fftn_blas(a, mesh)
    def _ifftn_wrapper(a):  # noqa
        mesh = a.shape[1:]
        return _ifftn_blas(a, mesh)


def fft(f, mesh):
    '''Perform the 3D FFT from real (R) to reciprocal (G) space.

    After FFT, (u, v, w) -> (j, k, l).
    (jkl) is in the index order of Gv.

    FFT normalization factor is 1., as in MH and in `numpy.fft`.

    Args:
        f : (nx*ny*nz,) ndarray
            The function to be FFT'd, flattened to a 1D array corresponding
            to the index order of :func:`cartesian_prod`.
        mesh : (3,) ndarray of ints (= nx,ny,nz)
            The number G-vectors along each direction.

    Returns:
        (nx*ny*nz,) ndarray
            The FFT 1D array in same index order as Gv (natural order of
            numpy.fft).

    '''
    if f.size == 0:
        return np.zeros_like(f)

    f3d = f.reshape(-1, *mesh)
    assert (f3d.shape[0] == 1 or f[0].size == f3d[0].size)
    g3d = _fftn_wrapper(f3d)
    ngrids = np.prod(mesh)
    if f.ndim == 1 or (f.ndim == 3 and f.size == ngrids):
        return g3d.ravel()
    else:
        return g3d.reshape(-1, ngrids)

def ifft(g, mesh):
    '''Perform the 3D inverse FFT from reciprocal (G) space to real (R) space.

    Inverse FFT normalization factor is 1./N, same as in `numpy.fft` but
    **different** from MH (they use 1.).

    Args:
        g : (nx*ny*nz,) ndarray
            The function to be inverse FFT'd, flattened to a 1D array
            corresponding to the index order of `span3`.
        mesh : (3,) ndarray of ints (= nx,ny,nz)
            The number G-vectors along each direction.

    Returns:
        (nx*ny*nz,) ndarray
            The inverse FFT 1D array in same index order as Gv (natural order
            of numpy.fft).

    '''
    if g.size == 0:
        return np.zeros_like(g)

    g3d = g.reshape(-1, *mesh)
    assert (g3d.shape[0] == 1 or g[0].size == g3d[0].size)
    f3d = _ifftn_wrapper(g3d)
    ngrids = np.prod(mesh)
    if g.ndim == 1 or (g.ndim == 3 and g.size == ngrids):
        return f3d.ravel()
    else:
        return f3d.reshape(-1, ngrids)


def fftk(f, mesh, expmikr):
    r'''Perform the 3D FFT of a real-space function which is (periodic*e^{ikr}).

    fk(k+G) = \sum_r fk(r) e^{-i(k+G)r} = \sum_r [f(k)e^{-ikr}] e^{-iGr}
    '''
    return fft(f*expmikr, mesh)


def ifftk(g, mesh, expikr):
    r'''Perform the 3D inverse FFT of f(k+G) into a function which is (periodic*e^{ikr}).

    fk(r) = (1/Ng) \sum_G fk(k+G) e^{i(k+G)r} = (1/Ng) \sum_G [fk(k+G)e^{iGr}] e^{ikr}
    '''
    return ifft(g, mesh) * expikr


def get_coulG(cell, k=np.zeros(3), exx=False, mf=None, mesh=None, Gv=None,
              wrap_around=True, omega=None, **kwargs):
    '''Calculate the Coulomb kernel for all G-vectors, handling G=0 and exchange.

    Args:
        k : (3,) ndarray
            k-point
        exx : bool or str
            Whether this is an exchange matrix element
        mf : instance of :class:`SCF`

    Returns:
        coulG : (ngrids,) ndarray
            The Coulomb kernel.
        mesh : (3,) ndarray of ints (= nx,ny,nz)
            The number G-vectors along each direction.
        omega : float
            Enable Coulomb kernel ``erf(|omega|*r12)/r12`` if omega > 0
            and ``erfc(|omega|*r12)/r12`` if omega < 0.
            Note this parameter is slightly different to setting cell.omega for
            exxdiv='ewald' at G0. When cell.omega is configured, the Ewald probe
            charge correction will be computed using the LR or SR Coulomb
            interactions. However, when this kwarg is explicitly specified, the
            exxdiv correction is computed with the full-range Coulomb
            interaction (1/r12). This parameter should only be specified in the
            range-separated JK builder and range-separated DF (and other
            range-separated integral methods if any).
    '''
    exxdiv = exx
    if isinstance(exx, str):
        exxdiv = exx
    elif exx and mf is not None:
        exxdiv = mf.exxdiv

    if mesh is None:
        mesh = cell.mesh
    if 'gs' in kwargs:
        warnings.warn('cell.gs is deprecated.  It is replaced by cell.mesh,'
                      'the number of PWs (=2*gs+1) along each direction.')
        mesh = [2*n+1 for n in kwargs['gs']]
    if Gv is None:
        Gv = cell.get_Gv(mesh)

    if omega is None:
        _omega = cell.omega
    else:
        _omega = omega

    if cell.dimension == 0 and cell.low_dim_ft_type != 'inf_vacuum':
        a = cell.lattice_vectors()
        assert abs(np.eye(3)*a[0,0] - a).max() < 1e-6, \
                'Must be cubic box for cell.dimension=0'
        # ensure the sphere is completely inside the box
        Rc = a[0,0] / 2

        # The truncated Coulomb kernel is
        #    \int_0^R e^{-iG dot r} 1/r dr^3 = 4pi/G \int_0^R sin(G r) dr
        #        = 4pi/G^2 (1 - cos(G R))
        # When R->infinity, the truncated Coulomb becomes the full-range
        # Coulomb. Its kernel is 4pi/G^2. Comparing the two integrals, the
        # divergentintegral
        #    4pi/G^2 \int_R^\inf sin(G R) dr = \int_0^\inf - \int_R^\inf ...
        # would give a regularized value
        #    4pi/G^2 cos (G R)
        # The long-range truncated Coulomb kernel
        #    \int_0^R erf(omega r)/r dr^3 = \int_0^\inf - \int_R^\inf ...
        #        = 4pi/G^2 exp(-G^2/(4 omega^2)) - 4pi/G \int_R^\inf erf(omega r) sin(G r) dr
        # If erf(omega R) ~= 1 for sufficient large R, the second term is
        # simplified to the sin(G r) regularized integral. The long range
        # truncated Coulomb is then given by
        #    4pi/G^2 (exp(-G^2/(4 omega^2) - cos(G R))
        # The short range part is
        #    4pi/G^2 (1 - exp(-G^2/(4 omega^2))
        if (_omega != 0 and
            abs(_omega) * Rc < 2.0): # typically, error of \int erf(omega r) sin (G r) < 1e-5
            raise RuntimeError(
                'In sufficient box size for the truncated range-separated '
                'Coulomb potential in 0D case')

        absG = np.linalg.norm(Gv, axis=1)
        with np.errstate(divide='ignore',invalid='ignore'):
            coulG = 4*np.pi/absG**2
            coulG[0] = 0
        if _omega == 0:
            coulG *= 1. - np.cos(absG*Rc)
            # G=0 term carries the charge. This special term supports the charged
            # system for dimension=0.
            coulG[0] = 2*np.pi*Rc**2
        elif _omega > 0:
            coulG *= np.exp(-.25/_omega**2 * absG**2) - np.cos(absG*Rc)
            coulG[0] = 2*np.pi*Rc**2 - np.pi / _omega**2
        else:
            coulG *= 1 - np.exp(-.25/_omega**2 * absG**2)
            coulG[0] = np.pi / _omega**2
        return coulG

    if abs(k).sum() > 1e-9:
        kG = k + Gv
    else:
        kG = Gv

    equal2boundary = None
    if wrap_around and abs(k).sum() > 1e-9:
        equal2boundary = np.zeros(Gv.shape[0], dtype=bool)
        # Here we 'wrap around' the high frequency k+G vectors into their lower
        # frequency counterparts.  Important if you want the gamma point and k-point
        # answers to agree
        b = cell.reciprocal_vectors()
        box_edge = np.einsum('i,ij->ij', np.asarray(mesh)//2+0.5, b)
        assert (all(np.linalg.solve(box_edge.T, k).astype(int)==0))
        reduced_coords = np.linalg.solve(box_edge.T, kG.T).T
        on_edge_p1 = abs(reduced_coords - 1) < 1e-9
        on_edge_m1 = abs(reduced_coords + 1) < 1e-9
        if cell.dimension >= 1:
            equal2boundary |= on_edge_p1[:,0]
            equal2boundary |= on_edge_m1[:,0]
            kG[reduced_coords[:,0]> 1] -= 2 * box_edge[0]
            kG[reduced_coords[:,0]<-1] += 2 * box_edge[0]
        if cell.dimension >= 2:
            equal2boundary |= on_edge_p1[:,1]
            equal2boundary |= on_edge_m1[:,1]
            kG[reduced_coords[:,1]> 1] -= 2 * box_edge[1]
            kG[reduced_coords[:,1]<-1] += 2 * box_edge[1]
        if cell.dimension == 3:
            equal2boundary |= on_edge_p1[:,2]
            equal2boundary |= on_edge_m1[:,2]
            kG[reduced_coords[:,2]> 1] -= 2 * box_edge[2]
            kG[reduced_coords[:,2]<-1] += 2 * box_edge[2]

    absG2 = np.einsum('gi,gi->g', kG, kG)
    G0_idx = []

    if hasattr(mf, 'kpts'):
        kpts = mf.kpts
    else:
        kpts = k.reshape(1,3)
    Nk = len(kpts)

    if exxdiv == 'vcut_sph':  # PRB 77 193110
        Rc = (3*Nk*cell.vol/(4*np.pi))**(1./3)
        with np.errstate(divide='ignore',invalid='ignore'):
            coulG = 4*np.pi/absG2*(1.0 - np.cos(np.sqrt(absG2)*Rc))
        coulG[absG2==0] = 4*np.pi*0.5*Rc**2

        if cell.dimension < 3:
            raise NotImplementedError

    elif exxdiv == 'vcut_ws':  # PRB 87, 165122
        assert (cell.dimension == 3)
        if not getattr(mf, '_ws_exx', None):
            mf._ws_exx = precompute_exx(cell, kpts)
        exx_alpha = mf._ws_exx['alpha']
        exx_kcell = mf._ws_exx['kcell']
        exx_q = mf._ws_exx['q']
        exx_vq = mf._ws_exx['vq']

        with np.errstate(divide='ignore',invalid='ignore'):
            coulG = 4*np.pi/absG2*(1.0 - np.exp(-absG2/(4*exx_alpha**2)))
        coulG[absG2==0] = np.pi / exx_alpha**2
        # Index k+Gv into the precomputed vq and add on
        gxyz = np.dot(kG, exx_kcell.lattice_vectors().T)/(2*np.pi)
        gxyz = gxyz.round(decimals=6).astype(int)
        mesh = np.asarray(exx_kcell.mesh)
        gxyz = (gxyz + mesh)%mesh
        qidx = (gxyz[:,0]*mesh[1] + gxyz[:,1])*mesh[2] + gxyz[:,2]
        #qidx = [np.linalg.norm(exx_q-kGi,axis=1).argmin() for kGi in kG]
        maxqv = abs(exx_q).max(axis=0)
        is_lt_maxqv = (abs(kG) <= maxqv).all(axis=1)
        coulG = coulG.astype(exx_vq.dtype)
        coulG[is_lt_maxqv] += exx_vq[qidx[is_lt_maxqv]]

        if cell.dimension < 3:
            raise NotImplementedError

    else:
        # Ewald probe charge method to get the leading term of the finite size
        # error in exchange integrals

        G0_idx = np.where(absG2==0)[0]
        if cell.dimension == 3 or cell.low_dim_ft_type == 'inf_vacuum':
            with np.errstate(divide='ignore'):
                coulG = 4*np.pi/absG2
                coulG[G0_idx] = 0

        elif cell.dimension == 2:
            # The following 2D analytical fourier transform is taken from:
            # R. Sundararaman and T. Arias PRB 87, 2013
            b = cell.reciprocal_vectors()
            Ld2 = np.pi/np.linalg.norm(b[2])
            Gz = kG[:,2]
            Gp = np.linalg.norm(kG[:,:2], axis=1)
            weights = 1. - np.cos(Gz*Ld2) * np.exp(-Gp*Ld2)
            with np.errstate(divide='ignore', invalid='ignore'):
                coulG = weights*4*np.pi/absG2
            if len(G0_idx) > 0:
                coulG[G0_idx] = -2*np.pi*Ld2**2 #-pi*L_z^2/2

        elif cell.dimension == 1:
            logger.warn(cell, 'No method for PBC dimension 1, dim-type %s.'
                        '  cell.low_dim_ft_type="inf_vacuum"  should be set.',
                        cell.low_dim_ft_type)
            raise NotImplementedError('truncated coulG for dimension=1 is numerically inaccurate')

            # Carlo A. Rozzi, PRB 73, 205119 (2006)
            a = cell.lattice_vectors()
            # Rc is the cylindrical radius
            Rc = np.sqrt(cell.vol / np.linalg.norm(a[0])) / 2
            Gx = abs(kG[:,0])
            Gp = np.linalg.norm(kG[:,1:], axis=1)
            with np.errstate(divide='ignore', invalid='ignore'):
                weights = 1 + Gp*Rc * scipy.special.j1(Gp*Rc) * scipy.special.k0(Gx*Rc)
                weights -= Gx*Rc * scipy.special.j0(Gp*Rc) * scipy.special.k1(Gx*Rc)
                coulG = 4*np.pi/absG2 * weights
                # TODO: numerical integration
                # coulG[Gx==0] = -4*np.pi * (dr * r * scipy.special.j0(Gp*r) * np.log(r)).sum()
            if len(G0_idx) > 0:
                coulG[G0_idx] = -np.pi*Rc**2 * (2*np.log(Rc) - 1)
        else:
            raise NotImplementedError(f'dimension={cell.dimension} with '
                                      f'low_dim_ft_type={cell.low_dim_ft_type} is not supported')

    if equal2boundary is not None:
        coulG[equal2boundary] = 0

    # Scale the coulG kernel for attenuated Coulomb integrals.
    # * kwarg omega is used by RangeSeparatedJKBuilder which requires ewald probe charge
    # being evaluated with regular Coulomb interaction (1/r12).
    # * cell.omega, which affects the ewald probe charge, is often set by
    # DFT-RSH functionals to build long-range HF-exchange for erf(omega*r12)/r12
    if _omega != 0 and cell.dimension != 3:
        logger.warn(cell, 'The coulG kernel for range-separated Coulomb potential '
                    f'for PBC {cell.dimension} is inaccurate.')
    if _omega > 0:
        # long range part
        coulG *= np.exp(-.25/_omega**2 * absG2)
    elif _omega < 0:
        if exxdiv == 'vcut_sph' or exxdiv == 'vcut_ws':
            raise RuntimeError(f'SR Coulomb for exxdiv={exxdiv} is not available')
        # short range part
        coulG *= (1 - np.exp(-.25/_omega**2 * absG2))

    # For full-range Coulomb and long-range Coulomb,
    # the divergent part of periodic summation of (ii|ii) integrals in
    # Coulomb integrals were cancelled out by electron-nucleus
    # interaction. The periodic part of (ii|ii) in exchange cannot be
    # cancelled out by Coulomb integrals. Its leading term is calculated
    # using Ewald probe charge (the function madelung below)
    if cell.dimension > 0 and exxdiv == 'ewald' and len(G0_idx) > 0:
        if omega is None: # Affects DFT-RSH
            coulG[G0_idx] += Nk*cell.vol*madelung(cell, kpts)
        else: # for RangeSeparatedJKBuilder
            coulG[G0_idx] += Nk*cell.vol*madelung(cell, kpts, omega=0)
    return coulG

def precompute_exx(cell, kpts):
    from pyscf.pbc import gto as pbcgto
    from pyscf.pbc.dft import gen_grid
    log = lib.logger.Logger(cell.stdout, cell.verbose)
    log.debug("# Precomputing Wigner-Seitz EXX kernel")
    Nk = get_monkhorst_pack_size(cell, kpts)
    log.debug("# Nk = %s", Nk)

    kcell = pbcgto.Cell()
    kcell.atom = 'H 0. 0. 0.'
    kcell.spin = 1
    kcell.unit = 'B'
    kcell.verbose = 0
    kcell.a = cell.lattice_vectors() * Nk
    Lc = 1.0/lib.norm(np.linalg.inv(kcell.a), axis=0)
    log.debug("# Lc = %s", Lc)
    Rin = Lc.min() / 2.0
    log.debug("# Rin = %s", Rin)
    # ASE:
    alpha = 5./Rin # sqrt(-ln eps) / Rc, eps ~ 10^{-11}
    log.info("WS alpha = %s", alpha)
    kcell.mesh = np.array([4*int(L*alpha*3.0) for L in Lc])  # ~ [120,120,120]
    # QE:
    #alpha = 3./Rin * np.sqrt(0.5)
    #kcell.mesh = (4*alpha*np.linalg.norm(kcell.a,axis=1)).astype(int)
    log.debug("# kcell.mesh FFT = %s", kcell.mesh)
    rs = kcell.get_uniform_grids(wrap_around=False)
    kngs = len(rs)
    log.debug("# kcell kngs = %d", kngs)
    corners_coord = lib.cartesian_prod(([0, 1], [0, 1], [0, 1]))
    corners = np.dot(corners_coord, kcell.a)
    #vR = np.empty(kngs)
    #for i, rv in enumerate(rs):
    #    # Minimum image convention to corners of kcell parallelepiped
    #    r = lib.norm(rv-corners, axis=1).min()
    #    if np.isclose(r, 0.):
    #        vR[i] = 2*alpha / np.sqrt(np.pi)
    #    else:
    #        vR[i] = scipy.special.erf(alpha*r) / r
    r = np.min([lib.norm(rs-c, axis=1) for c in corners], axis=0)
    vR = scipy.special.erf(alpha*r) / (r+1e-200)
    vR[r<1e-9] = 2*alpha / np.sqrt(np.pi)
    vG = (kcell.vol/kngs) * fft(vR, kcell.mesh)

    if abs(vG.imag).max() > 1e-6:
        # vG should be real in regular lattice. If imaginary part is observed,
        # this probably means a ws cell was built from a unconventional
        # lattice. The SR potential erfc(alpha*r) for the charge in the center
        # of ws cell decays to the region out of ws cell. The Ewald-sum based
        # on the minimum image convention cannot be used to build the kernel
        # Eq (12) of PRB 87, 165122
        raise RuntimeError('Unconventional lattice was found')

    ws_exx = {'alpha': alpha,
              'kcell': kcell,
              'q'    : kcell.Gv,
              'vq'   : vG.real.copy()}
    log.debug("# Finished precomputing")
    return ws_exx


def madelung(cell, kpts, omega=None):
    Nk = get_monkhorst_pack_size(cell, kpts)
    ecell = cell.copy(deep=False)
    ecell._atm = np.array([[1, cell._env.size, 0, 0, 0, 0]])
    ecell._env = np.append(cell._env, [0., 0., 0.])
    ecell.unit = 'B'
    #ecell.verbose = 0
    ecell.a = a = np.einsum('xi,x->xi', cell.lattice_vectors(), Nk)

    if omega is None:
        omega = cell.omega

    if omega == 0:
        return -2*ecell.ewald()

    else:
        # cell.ewald function does not use the Coulomb kernel function
        # get_coulG. When computing the nuclear interactions with attenuated
        # Coulomb operator, the Ewald summation technique is not needed
        # because the Coulomb kernel 4pi/G^2*exp(-G^2/4/omega**2) decays
        # quickly.
        precision = cell.precision
        Ecut = 10.
        Ecut = np.log(16*np.pi**2/(2*omega**2*(2*Ecut)**.5) / precision + 1.) * 2*omega**2
        Ecut = np.log(16*np.pi**2/(2*omega**2*(2*Ecut)**.5) / precision + 1.) * 2*omega**2
        mesh = cutoff_to_mesh(a, Ecut)
        Gv, Gvbase, weights = ecell.get_Gv_weights(mesh)
        wcoulG = get_coulG(ecell, Gv=Gv, omega=abs(omega), exxdiv=None) * weights
        SI = ecell.get_SI(mesh=mesh)
        ZSI = SI[0]
        e_lr = (2*abs(omega)/np.pi**0.5 -
                np.einsum('i,i,i->', ZSI.conj(), ZSI, wcoulG).real)
        if omega > 0:
            return e_lr
        else:
            e_fr = -2*ecell.ewald() # The full-range Coulomb
            return e_fr - e_lr


def get_monkhorst_pack_size(cell, kpts, tol=1e-5):
    kpts = np.reshape(kpts, (-1,3))
    min_tol = tol
    assert kpts.shape[0] < 1/min_tol
    if kpts.shape[0] == 1:
        Nk = np.array([1,1,1])
    else:
        tol = max(10**(-int(-np.log10(1/kpts.shape[0]))-2), min_tol)
        skpts = cell.get_scaled_kpts(kpts)
        Nk = np.array([np.count_nonzero(abs(ski[1:]-ski[:-1]) > tol) + 1
                       for ski in np.sort(skpts.T)])
    return Nk


def get_lattice_Ls(cell, nimgs=None, rcut=None, dimension=None, discard=True):
    '''Get the (Cartesian, unitful) lattice translation vectors for nearby images.
    The translation vectors can be used for the lattice summation.

    Kwargs:
        discard:
            Drop less important Ls based on AO values on grid
    '''
    if dimension is None:
        # For atoms near the boundary of the cell, it is necessary (even in low-
        # dimensional systems) to include lattice translations in all 3 dimensions.
        if cell.dimension < 2 or cell.low_dim_ft_type == 'inf_vacuum':
            dimension = cell.dimension
        else:
            dimension = 3
    if rcut is None:
        rcut = cell.rcut

    if dimension == 0 or rcut <= 0 or cell.natm == 0:
        return np.zeros((1, 3))

    a = cell.lattice_vectors()

    scaled_atom_coords = cell.get_scaled_atom_coords()
    atom_boundary_max = scaled_atom_coords[:,:dimension].max(axis=0)
    atom_boundary_min = scaled_atom_coords[:,:dimension].min(axis=0)
    ovlp_penalty = atom_boundary_max - atom_boundary_min
    dR = ovlp_penalty.dot(a[:dimension])
    dR_basis = np.diag(dR)

    # Search the minimal x,y,z requiring |x*a[0]+y*a[1]+z*a[2]+dR|^2 > rcut^2
    # Ls boundary should be derived by decomposing (a, Rij) for each atom-pair.
    # For reasons unclear, the so-obtained Ls boundary seems not large enough.
    # The upper-bound of the Ls boundary is generated by find_boundary function.
    def find_boundary(a):
        aR = np.vstack([a, dR_basis])
        r = np.linalg.qr(aR.T)[1]
        ub = (rcut + abs(r[2,3:]).sum()) / abs(r[2,2])
        return ub

    xb = find_boundary(a[[1,2,0]])
    if dimension > 1:
        yb = find_boundary(a[[2,0,1]])
    else:
        yb = 0
    if dimension > 2:
        zb = find_boundary(a)
    else:
        zb = 0
    bounds = np.ceil([xb, yb, zb]).astype(int)
    Ts = lib.cartesian_prod((np.arange(-bounds[0], bounds[0]+1),
                             np.arange(-bounds[1], bounds[1]+1),
                             np.arange(-bounds[2], bounds[2]+1)))
    Ls = np.dot(Ts[:,:dimension], a[:dimension])

    if discard and len(Ls) > 1:
        r = cell.atom_coords()
        rr = r[:,None] - r
        dist_max = np.linalg.norm(rr, axis=2).max()
        Ls_mask = np.linalg.norm(Ls, axis=1) < rcut + dist_max
        Ls = Ls[Ls_mask]
    return np.asarray(Ls, order='C')

def check_lattice_sum_range(cell, Ls):
    '''
    Evaluates whether the lattice summation range is sufficient.

    This function calculates the minimum distance between atoms in the primary
    unit cell and atoms in lattice images *not* included in the specified
    lattice sum vectors (Ls).
    '''
    Ls_full = get_lattice_Ls(cell, rcut=cell.rcut*1.5, discard=False)
    Ls_idx = intersection(Ls_full, Ls)
    Ls_remaining = np.setdiff1d(np.arange(len(Ls_full)), Ls_idx)
    atom_coords = cell.atom_coords()
    atoms_outside = (Ls_full[Ls_remaining,None] + atom_coords).reshape(-1, 3)
    return np.linalg.norm(atoms_outside[:,None] - atom_coords, axis=2).min()

def super_cell(cell, ncopy, wrap_around=False):
    '''Create an ncopy[0] x ncopy[1] x ncopy[2] supercell of the input cell
    Note this function differs from :func:`cell_plus_imgs` that cell_plus_imgs
    creates images in both +/- direction.

    Args:
        cell : instance of :class:`Cell`

        ncopy : (3,) array

        wrap_around : bool
            Put the original cell centered on the super cell. It has the
            effects corresponding to the parameter wrap_around of
            cell.make_kpts.

    Returns:
        supcell : instance of :class:`Cell`
    '''
    a = cell.lattice_vectors()
    #:supcell.atom = []
    #:for Lx in range(ncopy[0]):
    #:    for Ly in range(ncopy[1]):
    #:        for Lz in range(ncopy[2]):
    #:            # Using cell._atom guarantees coord is in Bohr
    #:            for atom, coord in cell._atom:
    #:                L = np.dot([Lx, Ly, Lz], a)
    #:                supcell.atom.append([atom, coord + L])
    xs = np.arange(ncopy[0])
    ys = np.arange(ncopy[1])
    zs = np.arange(ncopy[2])
    if wrap_around:
        xs[(ncopy[0]+1)//2:] -= ncopy[0]
        ys[(ncopy[1]+1)//2:] -= ncopy[1]
        zs[(ncopy[2]+1)//2:] -= ncopy[2]
    Ts = lib.cartesian_prod((xs, ys, zs))
    Ls = np.dot(Ts, a)
    supcell = cell.copy(deep=False)
    supcell.a = np.einsum('i,ij->ij', ncopy, a)
    mesh = np.asarray(ncopy) * np.asarray(cell.mesh)
    supcell.mesh = (mesh // 2) * 2 + 1
    if isinstance(cell.magmom, np.ndarray):
        supcell.magmom = cell.magmom.tolist() * np.prod(ncopy)
    else:
        supcell.magmom = cell.magmom * np.prod(ncopy)
    return _build_supcell_(supcell, cell, Ls)


def cell_plus_imgs(cell, nimgs):
    '''Create a supercell via nimgs[i] in each +/- direction, as in get_lattice_Ls().
    Note this function differs from :func:`super_cell` that super_cell only
    stacks the images in + direction.

    Args:
        cell : instance of :class:`Cell`
        nimgs : (3,) array

    Returns:
        supcell : instance of :class:`Cell`
    '''
    a = cell.lattice_vectors()
    Ts = lib.cartesian_prod((np.arange(-nimgs[0], nimgs[0]+1),
                             np.arange(-nimgs[1], nimgs[1]+1),
                             np.arange(-nimgs[2], nimgs[2]+1)))
    Ls = np.dot(Ts, a)
    supcell = cell.copy(deep=False)
    supcell.a = np.einsum('i,ij->ij', nimgs, a)
    supcell.mesh = np.array([(nimgs[0]*2+1)*cell.mesh[0],
                             (nimgs[1]*2+1)*cell.mesh[1],
                             (nimgs[2]*2+1)*cell.mesh[2]])
    return _build_supcell_(supcell, cell, Ls)

def _build_supcell_(supcell, cell, Ls):
    '''
    Construct supcell ._env directly without calling supcell.build() method.
    This reserves the basis contraction coefficients defined in cell
    '''
    from pyscf.pbc import gto as pbcgto
    nimgs = len(Ls)
    symbs = [atom[0] for atom in cell._atom] * nimgs
    coords = Ls.reshape(-1,1,3) + cell.atom_coords()
    coords = coords.reshape(-1,3)
    x, y, z = coords.T
    supcell.atom = supcell._atom = list(zip(symbs, zip(x, y, z)))
    supcell.unit = 'B'
    supcell.enuc = None # reset nuclear energy

    # Do not call supcell.build() to initialize supcell since it may normalize
    # the basis contraction coefficients

    # preserves environments defined in cell._env (e.g. omega, gauge origin)
    _env = np.append(cell._env, coords.ravel())
    _atm = np.repeat(cell._atm[None,:,:], nimgs, axis=0)
    _atm = _atm.reshape(-1, ATM_SLOTS)
    # Point to the coordinates appended to _env
    _atm[:,PTR_COORD] = cell._env.size + np.arange(nimgs * cell.natm) * 3

    _bas = np.repeat(cell._bas[None,:,:], nimgs, axis=0)
    # For atom pointers in each image, shift natm*image_id
    _bas[:,:,ATOM_OF] += np.arange(nimgs)[:,None] * cell.natm

    supcell._atm = np.asarray(_atm, dtype=np.int32)
    supcell._bas = np.asarray(_bas.reshape(-1, BAS_SLOTS), dtype=np.int32)
    supcell._env = _env

    if isinstance(supcell, pbcgto.Cell) and getattr(supcell, 'space_group_symmetry', False):
        supcell.build_lattice_symmetry(not cell._mesh_from_build)
    return supcell


def cutoff_to_mesh(a, cutoff):
    r'''
    Convert KE cutoff to FFT-mesh

        uses KE = k^2 / 2, where k_max ~ \pi / grid_spacing

    Args:
        a : (3,3) ndarray
            The real-space cell lattice vectors. Each row represents a
            lattice vector.
        cutoff : float
            KE energy cutoff in a.u.

    Returns:
        mesh : (3,) array
    '''
    # Search the minimal x,y,z requiring |x*b[0]+y*b[1]+z*b[2]|^2 > 2 * cutoff
    b = 2 * np.pi * np.linalg.inv(a.T)
    rx = np.linalg.qr(b[[1,2,0]].T)[1][2,2]
    ry = np.linalg.qr(b[[2,0,1]].T)[1][2,2]
    rz = np.linalg.qr(b.T)[1][2,2]

    Gmax = (2*cutoff)**.5 / np.abs([rx, ry, rz])
    mesh = np.ceil(Gmax).astype(int) * 2 + 1
    return mesh

def mesh_to_cutoff(a, mesh):
    '''
    Convert #grid points to KE cutoff
    '''
    # Search the minimal x,y,z requiring |x*b[0]+y*b[1]+z*b[2]|^2 > 2 * cutoff
    b = 2 * np.pi * np.linalg.inv(a.T)
    rx = np.linalg.qr(b[[1,2,0]].T)[1][2,2]
    ry = np.linalg.qr(b[[2,0,1]].T)[1][2,2]
    rz = np.linalg.qr(b.T)[1][2,2]

    gs = (np.asarray(mesh) - 1) // 2
    Gmax = gs * np.array([rx, ry, rz])
    ke_cutoff = Gmax**2 / 2
    return ke_cutoff

def cutoff_to_gs(a, cutoff):
    '''Deprecated.  Replaced by function cutoff_to_mesh.'''
    return [n//2 for n in cutoff_to_mesh(a, cutoff)]

def gs_to_cutoff(a, gs):
    '''Deprecated.  Replaced by function mesh_to_cutoff.'''
    return mesh_to_cutoff(a, [2*n+1 for n in gs])

def round_to_cell0(r, tol=1e-6):
    '''Round scaled coordinates to reference unit cell
    '''
    from pyscf.pbc.lib import kpts_helper
    return kpts_helper.round_to_fbz(r, wrap_around=False, tol=tol)
