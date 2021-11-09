import warnings
import math
import ctypes
from functools import partial
import numpy
from jax import custom_jvp
from pyscf.dft import libxc
from pyscf.dft.libxc import parse_xc, is_lda, is_meta_gga
from pyscfad.lib import numpy as jnp

def eval_xc(xc_code, rho, spin=0, relativity=0, deriv=1, omega=None, verbose=None):
    hyb, fn_facs = parse_xc(xc_code)
    if omega is not None:
        hyb[2] = float(omega)
    return _eval_xc(rho, hyb, fn_facs, spin, relativity, deriv, verbose)

@partial(custom_jvp, nondiff_argnums=tuple(range(1,7)))
def _eval_xc(rho, hyb, fn_facs, spin=0, relativity=0, deriv=1, verbose=None):
    return libxc._eval_xc(hyb, fn_facs, rho, spin, relativity, deriv, verbose)

@_eval_xc.defjvp
def _eval_xc_jvp(hyb, fn_facs, spin, relativity, deriv, verbose,
                 primals, tangents):
    rho,   = primals
    rho_t, = tangents

    if deriv > 2:
        raise NotImplementedError

    exc, vxc, fxc, kxc = _eval_xc(rho, hyb, fn_facs, spin, relativity, deriv+1, verbose)

    fn_ids = [x[0] for x in fn_facs]
    n      = len(fn_ids)

    fn_is_lda      = n == 0 or all((is_lda(x) for x in fn_ids))
    fn_is_meta_gga = any((is_meta_gga(x) for x in fn_ids))

    if fn_is_lda:
        if spin == 0:
            exc_jvp = (vxc[0] - exc) / rho * rho_t
            vxc_jvp = (fxc[0] * rho_t, None, None, None)
        else:
            rho_u   = rho[0]
            rho_d   = rho[1]
            rho_t_u = rho_t[0]
            rho_t_d = rho_t[1]

            vxc_u = vxc[0][:, 0]
            vxc_d = vxc[0][:, 1]

            fxc_uu = fxc[0][:, 0]
            fxc_ud = fxc[0][:, 1]
            fxc_dd = fxc[0][:, 2]

            exc_jvp_u = ((vxc_u - exc) / rho_u * rho_t_u)/2
            exc_jvp_d = ((vxc_d - exc) / rho_d * rho_t_d)/2

            vxc_jvp_u = (fxc_uu * rho_t_u + fxc_ud * rho_t_d)/2
            vxc_jvp_d = (fxc_dd * rho_t_d + fxc_ud * rho_t_u)/2

            exc_jvp = (exc_jvp_u + exc_jvp_d)
            vxc_jvp = (jnp.vstack((vxc_jvp_u, vxc_jvp_d)).T, None, None, None)

    elif fn_is_meta_gga:
        if spin == 0:
            exc1    = _exc_partial_deriv(rho, exc, vxc, "MGGA")
            exc_jvp = jnp.einsum('np,np->p', exc1, rho_t)

            vrho1, vsigma1, vlapl1, vtau1 = _vxc_partial_deriv(rho, exc, vxc, fxc, "MGGA")
            vrho_jvp   = jnp.einsum('np,np->p', vrho1, rho_t)
            vsigma_jvp = jnp.einsum('np,np->p', vsigma1, rho_t)
            vlapl_jvp  = jnp.einsum('np,np->p', vlapl1, rho_t)
            vtau_jvp   = jnp.einsum('np,np->p', vtau1, rho_t)
            
            vrho1 = vsigma1 = vlapl1 = vtau1 = None
            vxc_jvp = jnp.vstack((vrho_jvp, vsigma_jvp, vlapl_jvp, vtau_jvp))
        else:
            raise NotImplementedError

    else:
        if spin == 0:
            #exc_jvp = (vxc[0] - exc) / rho[0] * rho_t[0]
            #exc_jvp += vxc[1] / rho[0] * 2. * jnp.einsum('np,np->p', rho[1:4], rho_t[1:4])
            exc1 = _exc_partial_deriv(rho, exc, vxc, "GGA")
            exc_jvp = jnp.einsum('np,np->p', exc1, rho_t)

            #vrho1 = fxc[0] * rho_t[0] + fxc[1] * 2. * jnp.einsum('np,np->p', rho[1:4], rho_t[1:4])
            #vsigma1 = fxc[1] * rho_t[0] + fxc[2] * 2. * jnp.einsum('np,np->p', rho[1:4], rho_t[1:4])
            #vxc_jvp = (vrho1, vsigma1, None, None)
            vrho1, vsigma1, _, _ = _vxc_partial_deriv(rho, exc, vxc, fxc, "GGA")
            vrho_jvp = jnp.einsum('np,np->p', vrho1, rho_t)
            vsigma_jvp = jnp.einsum('np,np->p', vsigma1, rho_t)
            vrho1 = vsigma1 = None
            vxc_jvp = (vrho_jvp, vsigma_jvp, None, None)
        else:
            raise NotImplementedError

    if deriv == 0:
        vxc = fxc = kxc = vxc_jvp = fxc_jvp = kxc_jvp = None
    elif deriv == 1:
        fxc = kxc = fxc_jvp = kxc_jvp = None
    elif deriv == 2:
        kxc = kxc_jvp = None

    return (exc, vxc, fxc, kxc), (exc_jvp, vxc_jvp, fxc_jvp, kxc_jvp)

def _exc_partial_deriv(rho, exc, vxc, xctype="LDA"):
    if xctype == "LDA":
        exc1 = (vxc[0] - exc) / rho
    elif xctype in ["GGA", "MGGA"]:
        exc1 = numpy.empty(rho.shape, dtype=rho.dtype)
        exc1[0] = (vxc[0] - exc) / rho[0]
        exc1[1:4] = vxc[1] / rho[0] * 2. * rho[1:4]
        if xctype == "MGGA":
            exc1[4] = vxc[2] / rho[0]
            exc1[5] = vxc[3] / rho[0]
    else:
        raise KeyError
    return exc1

def _vxc_partial_deriv(rho, exc, vxc, fxc, xctype="LDA"):
    vrho1 = vsigma1 = vlapl1 = vtau1 = None
    if xctype == "LDA":
        vrho1 = fxc[0]
    elif xctype in ["GGA", "MGGA"]:
        vrho1 = numpy.empty(rho.shape, dtype=rho.dtype)
        vrho1[0] = fxc[0]
        vrho1[1:4] = fxc[1] * 2. * rho[1:4]

        vsigma1 = numpy.empty(rho.shape, dtype=rho.dtype)
        vsigma1[0] = fxc[1]
        vsigma1[1:4] = fxc[2] * 2. * rho[1:4]

        if xctype == "MGGA":
            vrho1[4] = fxc[5]
            vrho1[5] = fxc[6]

            vsigma1[4] = fxc[8]
            vsigma1[5] = fxc[9]

            vlapl1 = numpy.empty(rho.shape, dtype=rho.dtype)
            vlapl1[0] = fxc[5]
            vlapl1[1:4] = fxc[8] * 2. * rho[1:4]
            vlapl1[4] = fxc[3]
            vlapl1[5] = fxc[7]

            vtau1 = numpy.empty(rho.shape, dtype=rho.dtype)
            vtau1[0] = fxc[6]
            vtau1[1:4] = fxc[9] * 2. * rho[1:4]
            vtau1[4] = fxc[7]
            vtau1[5] = fxc[4]
    else:
        raise KeyError
    return vrho1, vsigma1, vlapl1, vtau1
