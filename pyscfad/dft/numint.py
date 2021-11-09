import warnings
from functools import partial
import numpy
import jax
from jax import jit
from jax import custom_jvp
import pyscf
from pyscf.dft import numint
from pyscf.dft.numint import SWITCH_SIZE
from pyscf.dft.gen_grid import make_mask, BLKSIZE
from pyscfad.lib import numpy as jnp
from pyscfad.lib import ops
from pyscfad.lib import stop_grad
from . import libxc

libdft = pyscf.lib.load_library('libdft')

def eval_mat(mol, ao, weight, rho, vxc,
             non0tab=None, xctype='LDA', spin=0, verbose=None):
    xctype = xctype.upper()
    if xctype in ['LDA', 'HF']:
        ngrids, _ = ao.shape
    else:
        ngrids, _ = ao[0].shape

    if non0tab is None:
        non0tab = numpy.ones(((ngrids+BLKSIZE-1)//BLKSIZE,mol.nbas),
                             dtype=numpy.uint8)
    shls_slice = (0, mol.nbas)
    ao_loc = mol.ao_loc_nr()
    transpose_for_uks = False
    if xctype in ['LDA', 'HF']:
        if not getattr(vxc, 'ndim', None) == 2:
            vrho = vxc[0]
        else:
            vrho = vxc
        # *.5 because return mat + mat.T
        #:aow = numpy.einsum('pi,p->pi', ao, .5*weight*vrho)
        aow = _scale_ao(ao, .5*weight*vrho)
        mat = _dot_ao_ao(mol, ao, aow, non0tab, shls_slice, ao_loc)
    else:
        #wv = weight * vsigma * 2
        #aow  = numpy.einsum('pi,p->pi', ao[1], rho[1]*wv)
        #aow += numpy.einsum('pi,p->pi', ao[2], rho[2]*wv)
        #aow += numpy.einsum('pi,p->pi', ao[3], rho[3]*wv)
        #aow += numpy.einsum('pi,p->pi', ao[0], .5*weight*vrho)
        vrho, vsigma = vxc[:2]
        wv = jnp.empty((4,ngrids))
        if spin == 0:
            assert(vsigma is not None and rho.ndim==2)
            #wv[0]  = weight * vrho * .5
            #wv[1:4] = rho[1:4] * (weight * vsigma * 2)
            wv = ops.index_update(wv, ops.index[0], weight * vrho * .5)
            wv = ops.index_update(wv, ops.index[1:4], rho[1:4] * (weight * vsigma * 2))
        else:
            rho_a, rho_b = rho
            #wv[0]  = weight * vrho * .5
            wv = ops.index_update(wv, ops.index[0], weight * vrho * .5)
            try:
                #wv[1:4] = rho_a[1:4] * (weight * vsigma[0] * 2)  # sigma_uu
                #wv[1:4]+= rho_b[1:4] * (weight * vsigma[1])      # sigma_ud
                tmp = rho_a[1:4] * (weight * vsigma[0] * 2) + rho_b[1:4] * (weight * vsigma[1])
                wv = ops.index_update(wv, ops.index[1:4], tmp)
            except ValueError:
                warnings.warn('Note the output of libxc.eval_xc cannot be '
                              'directly used in eval_mat.\nvsigma from eval_xc '
                              'should be restructured as '
                              '(vsigma[:,0],vsigma[:,1])\n')
                transpose_for_uks = True
                vsigma = vsigma.T
                #wv[1:4] = rho_a[1:4] * (weight * vsigma[0] * 2)  # sigma_uu
                #wv[1:4]+= rho_b[1:4] * (weight * vsigma[1])      # sigma_ud
                tmp = rho_a[1:4] * (weight * vsigma[0] * 2) + rho_b[1:4] * (weight * vsigma[1])
                wv = ops.index_update(wv, ops.index[1:4], tmp)
        #:aow = numpy.einsum('npi,np->pi', ao[:4], wv)
        aow = _scale_ao(ao[:4], wv)
        mat = _dot_ao_ao(mol, ao[0], aow, non0tab, shls_slice, ao_loc)

# JCP 138, 244108 (2013); DOI:10.1063/1.4811270
# JCP 112, 7002 (2000); DOI:10.1063/1.481298
    if xctype == 'MGGA':
        vlapl, vtau = vxc[2:]

        if vlapl is None:
            vlapl = 0
        else:
            if spin != 0:
                if transpose_for_uks:
                    vlapl = vlapl.T
                vlapl = vlapl[0]
            XX, YY, ZZ = 4, 7, 9
            ao2 = ao[XX] + ao[YY] + ao[ZZ]
            #:aow = numpy.einsum('pi,p->pi', ao2, .5 * weight * vlapl, out=aow)
            aow = _scale_ao(ao2, .5 * weight * vlapl, out=None)
            mat += _dot_ao_ao(mol, ao[0], aow, non0tab, shls_slice, ao_loc)

        if spin != 0:
            if transpose_for_uks:
                vtau = vtau.T
            vtau = vtau[0]
        wv = weight * (.25*vtau + vlapl)
        #:aow = numpy.einsum('pi,p->pi', ao[1], wv, out=aow)
        aow = _scale_ao(ao[1], wv, out=None)
        mat += _dot_ao_ao(mol, ao[1], aow, non0tab, shls_slice, ao_loc)
        #:aow = numpy.einsum('pi,p->pi', ao[2], wv, out=aow)
        aow = _scale_ao(ao[2], wv, out=None)
        mat += _dot_ao_ao(mol, ao[2], aow, non0tab, shls_slice, ao_loc)
        #:aow = numpy.einsum('pi,p->pi', ao[3], wv, out=aow)
        aow = _scale_ao(ao[3], wv, out=None)
        mat += _dot_ao_ao(mol, ao[3], aow, non0tab, shls_slice, ao_loc)

    return mat + mat.T.conj()

def nr_rks(ni, mol, grids, xc_code, dms, relativity=0, hermi=0,
           max_memory=2000, verbose=None):
    xctype = ni._xc_type(xc_code)
    make_rho, nset, nao = ni._gen_rho_evaluator(mol, dms, hermi)

    shls_slice = (0, mol.nbas)
    ao_loc = mol.ao_loc_nr()

    nelec = [0]*nset
    excsum = [0]*nset
    vmat = [0]*nset
    aow = None
    if xctype == 'LDA':
        ao_deriv = 0
        for ao, mask, weight, coords \
                in ni.block_loop(mol, grids, nao, ao_deriv, max_memory):
            #aow = numpy.ndarray(ao.shape, order='F', buffer=aow)
            for idm in range(nset):
                rho = make_rho(idm, ao, mask, 'LDA')
                exc, vxc = ni.eval_xc(xc_code, rho, spin=0,
                                      relativity=relativity, deriv=1,
                                      verbose=verbose)[:2]
                vrho = vxc[0]
                den = rho * weight
                nelec[idm] += stop_grad(den).sum()
                excsum[idm] += jnp.dot(den, exc)
                # *.5 because vmat + vmat.T
                #:aow = numpy.einsum('pi,p->pi', ao, .5*weight*vrho, out=aow)
                aow = _scale_ao(ao, .5*weight*vrho, out=None)
                vmat[idm] += _dot_ao_ao(mol, ao, aow, mask, shls_slice, ao_loc)
                rho = exc = vxc = vrho = None
    elif xctype == 'GGA':
        ao_deriv = 1
        for ao, mask, weight, coords \
                in ni.block_loop(mol, grids, nao, ao_deriv, max_memory):
            #aow = numpy.ndarray(ao[0].shape, order='F', buffer=aow)
            for idm in range(nset):
                rho = make_rho(idm, ao, mask, 'GGA')
                exc, vxc = ni.eval_xc(xc_code, rho, spin=0,
                                      relativity=relativity, deriv=1,
                                      verbose=verbose)[:2]
                den = rho[0] * weight
                nelec[idm] += stop_grad(den).sum()
                excsum[idm] += jnp.dot(den, exc)
                # ref eval_mat function
                wv = _rks_gga_wv0(rho, vxc, weight)
                #:aow = numpy.einsum('npi,np->pi', ao, wv, out=aow)
                aow = _scale_ao(ao, wv, out=None)
                vmat[idm] += _dot_ao_ao(mol, ao[0], aow, mask, shls_slice, ao_loc)
                rho = exc = vxc = wv = None
    elif xctype == 'NLC':
        nlc_pars = ni.nlc_coeff(xc_code[:-6])
        if nlc_pars == [0,0]:
            raise NotImplementedError('VV10 cannot be used with %s. '
                                      'The supported functionals are %s' %
                                      (xc_code[:-6], ni.libxc.VV10_XC))
        ao_deriv = 1
        vvrho=numpy.empty([nset,4,0])
        vvweight=numpy.empty([nset,0])
        vvcoords=numpy.empty([nset,0,3])
        for ao, mask, weight, coords \
                in ni.block_loop(mol, grids, nao, ao_deriv, max_memory):
            ao = stop_grad(ao)
            rhotmp = numpy.empty([0,4,weight.size])
            weighttmp = numpy.empty([0,weight.size])
            coordstmp = numpy.empty([0,weight.size,3])
            for idm in range(nset):
                rho = make_rho(idm, ao, mask, 'GGA')
                rho = numpy.asarray(stop_grad(rho))
                rho = numpy.expand_dims(rho,axis=0)
                rhotmp = numpy.concatenate((rhotmp,rho),axis=0)
                weighttmp = numpy.concatenate((weighttmp,numpy.expand_dims(weight,axis=0)),axis=0)
                coordstmp = numpy.concatenate((coordstmp,numpy.expand_dims(coords,axis=0)),axis=0)
                rho = None
            vvrho = numpy.concatenate((vvrho,rhotmp),axis=2)
            vvweight = numpy.concatenate((vvweight,weighttmp),axis=1)
            vvcoords = numpy.concatenate((vvcoords,coordstmp),axis=1)
            rhotmp = weighttmp = coordstmp = None
        for ao, mask, weight, coords \
                in ni.block_loop(mol, grids, nao, ao_deriv, max_memory):
            #aow = numpy.ndarray(ao[0].shape, order='F', buffer=aow)
            for idm in range(nset):
                rho = make_rho(idm, ao, mask, 'GGA')
                exc, vxc = _vv10nlc(rho,coords,vvrho[idm],vvweight[idm],vvcoords[idm],nlc_pars)
                den = rho[0] * weight
                nelec[idm] += stop_grad(den).sum()
                excsum[idm] += jnp.dot(den, exc)
                # ref eval_mat function
                wv = _rks_gga_wv0(rho, vxc, weight)
                #:aow = numpy.einsum('npi,np->pi', ao, wv, out=aow)
                aow = _scale_ao(ao, wv, out=None)
                vmat[idm] += _dot_ao_ao(mol, ao[0], aow, mask, shls_slice, ao_loc)
                rho = exc = vxc = wv = None
        vvrho = vvweight = vvcoords = None
    elif xctype == 'MGGA':
        if any(x in xc_code.upper() for x in ('CC06', 'CS', 'BR89', 'MK00')):
            raise NotImplementedError('laplacian in meta-GGA method')
        ao_deriv = 2
        for ao, mask, weight, coords \
                in ni.block_loop(mol, grids, nao, ao_deriv, max_memory):
            #aow = numpy.ndarray(ao[0].shape, order='F', buffer=aow)
            for idm in range(nset):
                rho = make_rho(idm, ao, mask, 'MGGA')
                exc, vxc = ni.eval_xc(xc_code, rho, spin=0,
                                      relativity=relativity, deriv=1,
                                      verbose=verbose)[:2]
                # pylint: disable=W0612
                vrho, vsigma, vlapl, vtau = vxc[:4]
                den = rho[0] * weight
                nelec[idm] += stop_grad(den).sum()
                excsum[idm] += jnp.dot(den, exc)

                wv = _rks_gga_wv0(rho, vxc, weight)
                #:aow = numpy.einsum('npi,np->pi', ao[:4], wv, out=aow)
                aow = _scale_ao(ao[:4], wv, out=None)
                vmat[idm] += _dot_ao_ao(mol, ao[0], aow, mask, shls_slice, ao_loc)
# pylint: disable=W0511
# FIXME: .5 * .5   First 0.5 for v+v.T symmetrization.
# Second 0.5 is due to the Libxc convention tau = 1/2 \nabla\phi\dot\nabla\phi
                wv = (.5 * .5 * weight * vtau).reshape(-1,1)
                vmat[idm] += _dot_ao_ao(mol, ao[1], wv*ao[1], mask, shls_slice, ao_loc)
                vmat[idm] += _dot_ao_ao(mol, ao[2], wv*ao[2], mask, shls_slice, ao_loc)
                vmat[idm] += _dot_ao_ao(mol, ao[3], wv*ao[3], mask, shls_slice, ao_loc)
                rho = exc = vxc = vrho = wv = None

    for i in range(nset):
        vmat[i] = vmat[i] + vmat[i].conj().T
    nelec = numpy.asarray(nelec)
    excsum = jnp.asarray(excsum)
    vmat = jnp.asarray(vmat)
    if nset == 1:
        nelec = nelec[0]
        excsum = excsum[0]
        vmat = vmat[0]
    return nelec, excsum, vmat

def nr_uks(ni, mol, grids, xc_code, dms, relativity=0, hermi=0,
           max_memory=2000, verbose=None):

    xctype = ni._xc_type(xc_code)

    if xctype == 'NLC':
        dms_sf = dms[0] + dms[1]
        nelec, excsum, vmat = nr_rks(ni, mol, grids, xc_code, dms_sf, relativity, hermi, max_memory, verbose)
        return [nelec,nelec], excsum, jnp.asarray([vmat,vmat])

    dma, dmb = _format_uks_dm(dms)
    nao      = dma.shape[-1]
    make_rhoa, nset = ni._gen_rho_evaluator(mol, dma, hermi)[:2]
    make_rhob       = ni._gen_rho_evaluator(mol, dmb, hermi)[0]

    shls_slice = (0, mol.nbas)
    ao_loc     = mol.ao_loc_nr()

    nelec  = [[0]*nset for _ in range(2)]
    excsum = [0]*nset
    vmat   = [[0]*nset for _ in range(2)]
    aow    = None

    if xctype == 'LDA':
        ao_deriv = 0
        for ao, mask, weight, coords \
                in ni.block_loop(mol, grids, nao, ao_deriv, max_memory):
            #aow = numpy.ndarray(ao.shape, order='F', buffer=aow)
            for idm in range(nset):
                rho_a = make_rhoa(idm, ao, mask, "LDA")
                rho_b = make_rhob(idm, ao, mask, "LDA")

                exc, vxc = ni.eval_xc(xc_code, (rho_a, rho_b), spin=1,
                                      relativity=relativity, deriv=1,
                                      verbose=verbose)[:2]

                vrho = vxc[0]

                den            = rho_a * weight
                nelec[0][idm] += stop_grad(den).sum()
                excsum[idm]   += jnp.dot(den, exc)

                den            = rho_b * weight
                nelec[1][idm] += stop_grad(den).sum()
                excsum[idm]   += jnp.dot(den, exc)

                aow           = _scale_ao(ao, .5*weight*vrho[:,0], out=None)
                vmat[0][idm] += _dot_ao_ao(mol, ao, aow, mask, shls_slice, ao_loc)

                aow           = _scale_ao(ao, .5*weight*vrho[:,1], out=None)
                vmat[1][idm] += _dot_ao_ao(mol, ao, aow, mask, shls_slice, ao_loc)
                rho_a = rho_b = exc = vxc = vrho = None

    elif xctype == 'GGA':
        ao_deriv = 1
        for ao, mask, weight, coords \
                in ni.block_loop(mol, grids, nao, ao_deriv, max_memory):
            #aow = numpy.ndarray(ao[0].shape, order='F', buffer=aow)
            for idm in range(nset):
                rho_a = make_rhoa(idm, ao, mask, "GGA")
                rho_b = make_rhob(idm, ao, mask, "GGA")

                exc, vxc = ni.eval_xc(xc_code, (rho_a, rho_b), spin=1,
                                      relativity=relativity, deriv=1,
                                      verbose=verbose)[:2]

                vrho = vxc[0]

                den            = rho_a[0] * weight
                nelec[0][idm] += stop_grad(den).sum()
                excsum[idm]   += jnp.dot(den, exc)

                den            = rho_b[0] * weight
                nelec[1][idm] += stop_grad(den).sum()
                excsum[idm]   += jnp.dot(den, exc)

                wva, wvb      = _uks_gga_wv0((rho_a,rho_b), vxc, weight)

                aow           = _scale_ao(ao, wva, out=aow)
                vmat[0][idm] += _dot_ao_ao(mol, ao[0], aow, mask, shls_slice, ao_loc)

                aow           = _scale_ao(ao, wvb, out=aow)
                vmat[1][idm] += _dot_ao_ao(mol, ao[0], aow, mask, shls_slice, ao_loc)

                rho_a = rho_b = exc = vxc = wva = wvb = None

    elif xctype == 'MGGA':
        if any(x in xc_code.upper() for x in ('CC06', 'CS', 'BR89', 'MK00')):
            raise NotImplementedError('laplacian in meta-GGA method')
        ao_deriv = 2
        for ao, mask, weight, coords \
                in ni.block_loop(mol, grids, nao, ao_deriv, max_memory):
            #aow = numpy.ndarray(ao[0].shape, order='F', buffer=aow)
            for idm in range(nset):
                rho_a = make_rhoa(idm, ao, mask, xctype)
                rho_b = make_rhob(idm, ao, mask, xctype)

                exc, vxc = ni.eval_xc(xc_code, (rho_a, rho_b), spin=1,
                                      relativity=relativity, deriv=1,
                                      verbose=verbose)[:2]

                vrho, vsigma, vlapl, vtau = vxc[:4]

                den            = rho_a[0]*weight
                nelec[0][idm] += stop_grad(den).sum()
                excsum[idm]   += numpy.dot(den, exc)

                den            = rho_b[0]*weight
                nelec[1][idm] += stop_grad(den).sum()
                excsum[idm]   += numpy.dot(den, exc)

                wva, wvb      = _uks_gga_wv0((rho_a,rho_b), vxc, weight)

                aow           = _scale_ao(ao[:4], wva, out=aow)
                vmat[0][idm] += _dot_ao_ao(mol, ao[0], aow, mask, shls_slice, ao_loc)

                aow           = _scale_ao(ao[:4], wvb, out=aow)
                vmat[1][idm] += _dot_ao_ao(mol, ao[0], aow, mask, shls_slice, ao_loc)

                wv = (.25 * weight * vtau[:,0]).reshape(-1,1)
                vmat[0,idm] += _dot_ao_ao(mol, ao[1], wv*ao[1], mask, shls_slice, ao_loc)
                vmat[0,idm] += _dot_ao_ao(mol, ao[2], wv*ao[2], mask, shls_slice, ao_loc)
                vmat[0,idm] += _dot_ao_ao(mol, ao[3], wv*ao[3], mask, shls_slice, ao_loc)

                wv = (.25 * weight * vtau[:,1]).reshape(-1,1)
                vmat[1,idm] += _dot_ao_ao(mol, ao[1], wv*ao[1], mask, shls_slice, ao_loc)
                vmat[1,idm] += _dot_ao_ao(mol, ao[2], wv*ao[2], mask, shls_slice, ao_loc)
                vmat[1,idm] += _dot_ao_ao(mol, ao[3], wv*ao[3], mask, shls_slice, ao_loc)

                rho_a = rho_b = exc = vxc = vrho = wva = wvb = None

    elif xctype == 'HF':
        pass
    
    else:
        raise NotImplementedError(f'numint.nr_uks for functional {xc_code}')

    for i in range(nset):
        vmat[0][i] = (vmat[0][i] + vmat[0][i].conj().T)
        vmat[1][i] = (vmat[1][i] + vmat[1][i].conj().T)

    if isinstance(dma, jnp.ndarray) and dma.ndim == 2:
        excsum = excsum[0]
        nelec  = jnp.asarray([nelec[0], nelec[1]])
        vmat   = jnp.asarray([vmat[0][0], vmat[1][0]])

    return nelec, excsum, vmat

def _format_uks_dm(dms):
    if isinstance(dms, jnp.ndarray) and dms.ndim == 2:  # RHF DM
        dma = dmb = dms * .5
    else:
        dma, dmb = dms
    return dma, dmb

def eval_rho(mol, ao, dm, non0tab=None, xctype='LDA', hermi=0, verbose=None):
    xctype = xctype.upper()
    if xctype in ('LDA', 'HF'):
        ngrids = ao.shape[0]
    else:
        ngrids = ao[0].shape[0]

    if non0tab is None:
        non0tab = numpy.ones(((ngrids+BLKSIZE-1)//BLKSIZE,mol.nbas),
                             dtype=numpy.uint8)
    if not hermi:
        # (D + D.T)/2 because eval_rho computes 2*(|\nabla i> D_ij <j|) instead of
        # |\nabla i> D_ij <j| + |i> D_ij <\nabla j| for efficiency
        dm = (dm + dm.conj().T) * .5

    shls_slice = (0, mol.nbas)
    ao_loc = mol.ao_loc_nr()
    if xctype in ('LDA', 'HF'):
        c0 = _dot_ao_dm(mol, ao, dm, non0tab, shls_slice, ao_loc)
        #:rho = numpy.einsum('pi,pi->p', ao, c0)
        rho = _contract_rho(ao, c0)
    elif xctype in ('GGA', 'NLC'):
        rho = jnp.empty((4,ngrids))
        #c0 = _dot_ao_dm(mol, ao[0], dm, non0tab, shls_slice, ao_loc)
        #:rho[0] = numpy.einsum('pi,pi->p', c0, ao[0])
        #rho = ops.index_update(rho, ops.index[0], _contract_rho(c0, ao[0]))
        #for i in range(1, 4):
        #    #:rho[i] = numpy.einsum('pi,pi->p', c0, ao[i])
        #    rho = ops.index_update(rho, ops.index[i], _contract_rho(c0, ao[i]) * 2)
        rho = _rks_gga_assemble_rho(rho, ao, dm)
    else: # meta-GGA
        # rho[4] = \nabla^2 rho, rho[5] = 1/2 |nabla f|^2
        rho = jnp.empty((6,ngrids))
        #c0 = _dot_ao_dm(mol, ao[0], dm, non0tab, shls_slice, ao_loc)
        #:rho[0] = numpy.einsum('pi,pi->p', ao[0], c0)
        #rho = ops.index_update(rho, ops.index[0], _contract_rho(ao[0], c0))
        #rho = ops.index_update(rho, ops.index[5], 0)
        #for i in range(1, 4):
        #    #:rho[i] = numpy.einsum('pi,pi->p', c0, ao[i]) * 2 # *2 for +c.c.
        #    rho = ops.index_update(rho, ops.index[i], _contract_rho(c0, ao[i]) * 2)
        #    c1 = _dot_ao_dm(mol, ao[i], dm.T, non0tab, shls_slice, ao_loc)
        #    #:rho[5] += numpy.einsum('pi,pi->p', c1, ao[i])
        #    rho = ops.index_add(rho, ops.index[5], _contract_rho(c1, ao[i]))
        #XX, YY, ZZ = 4, 7, 9
        #ao2 = ao[XX] + ao[YY] + ao[ZZ]
        ##:rho[4] = numpy.einsum('pi,pi->p', c0, ao2)
        #rho = ops.index_update(rho, ops.index[4], _contract_rho(c0, ao2))
        #rho = ops.index_add(rho, ops.index[4], rho[5])
        #rho = ops.index_mul(rho, ops.index[4], 2)
        #rho = ops.index_mul(rho, ops.index[5], .5)
        rho = _rks_mgga_assemble_rho(rho, ao, dm)
    return rho

@jit
def _rks_gga_assemble_rho(rho, ao, dm):
    c0  = _dot_ao_dm_incore(ao[0], dm)
    rho = ops.index_update(rho, ops.index[0], _contract_rho(c0, ao[0]))
    for i in range(1, 4):
        rho = ops.index_update(rho, ops.index[i], _contract_rho(c0, ao[i]) * 2)
    return rho

@jit
def _rks_mgga_assemble_rho(rho, ao, dm):
    c0 = _dot_ao_dm_incore(ao[0], dm)
    rho = ops.index_update(rho, ops.index[0], _contract_rho(ao[0], c0))
    rho = ops.index_update(rho, ops.index[5], 0)
    for i in range(1, 4):
        rho = ops.index_update(rho, ops.index[i], _contract_rho(c0, ao[i]) * 2)
        c1 = _dot_ao_dm_incore(ao[i], dm.T)
        rho = ops.index_add(rho, ops.index[5], _contract_rho(c1, ao[i]))
    XX, YY, ZZ = 4, 7, 9
    ao2 = ao[XX] + ao[YY] + ao[ZZ]
    rho = ops.index_update(rho, ops.index[4], _contract_rho(c0, ao2))
    rho = ops.index_add(rho, ops.index[4], rho[5])
    rho = ops.index_mul(rho, ops.index[4], 2)
    rho = ops.index_mul(rho, ops.index[5], .5)
    return rho

@jit
def _scale_ao(ao, wv, out=None):
    #:aow = numpy.einsum('npi,np->pi', ao[:4], wv)
    if wv.ndim == 2:
        ao = ao.transpose(0,2,1)
    else:
        ngrids, nao = ao.shape
        ao = ao.T.reshape(1,nao,ngrids)
        wv = wv.reshape(1,ngrids)

    aow = jnp.einsum('nip,np->pi', ao, wv)
    return aow

def _dot_ao_ao(mol, ao1, ao2, non0tab, shls_slice, ao_loc, hermi=0):
    '''return numpy.dot(ao1.T, ao2)'''
    nao = ao1.shape[-1]
    if nao < SWITCH_SIZE:
        return _dot_ao_ao_incore(ao1, ao2)
    else:
        raise NotImplementedError

@jit
def _dot_ao_ao_incore(ao1, ao2):
    return jnp.dot(ao1.T.conj(), ao2)

def _dot_ao_dm(mol, ao, dm, non0tab, shls_slice, ao_loc, out=None):
    '''return numpy.dot(ao, dm)'''
    nao = ao.shape[-1]
    if nao < SWITCH_SIZE:
        return _dot_ao_dm_incore(ao, dm)
    else:
        raise NotImplementedError

@jit
def _dot_ao_dm_incore(ao, dm):
    return jnp.dot(jnp.asarray(dm).T, ao.T).T

@jit
def _contract_rho(bra, ket):
    bra = bra.T
    ket = ket.T

    rho  = jnp.einsum('ip,ip->p', bra.real, ket.real)
    rho += jnp.einsum('ip,ip->p', bra.imag, ket.imag)
    return rho

@jit
def _rks_gga_wv0(rho, vxc, weight):
    vrho, vgamma = vxc[:2]
    ngrid = vrho.size
    wv = jnp.empty((4,ngrid))
    wv = ops.index_update(wv, ops.index[0], weight * vrho * .5)
    wv = ops.index_update(wv, ops.index[1:], (weight * vgamma * 2) * rho[1:4])
    #wv = ops.index_mul(wv, ops.index[0], .5)  # v+v.T should be applied in the caller
    return wv

@jit
def _uks_gga_wv0(rho, vxc, weight):
    rhoa, rhob   = rho
    vrho, vsigma = vxc[:2]
    ngrid        = vrho.shape[0]

    wva     = jnp.empty((4, ngrid))
    wva = ops.index_update(wva, ops.index[0], weight * vrho[:,0] * .5)
    wva = ops.index_update(wva, ops.index[1:], (weight * vsigma[:,0] * 2) * rhoa[1:4] + (weight * vsigma[:,1]) * rhob[1:4])

    wvb     = jnp.empty((4, ngrid))
    wvb = ops.index_update(wvb, ops.index[0], weight * vrho[:,1] * .5)
    wvb = ops.index_update(wvb, ops.index[1:], (weight * vsigma[:,1] * 2) * rhob[1:4] + (weight * vsigma[:,0]) * rhoa[1:4])

    return wva, wvb

@partial(custom_jvp, nondiff_argnums=(1,2,3,4,5,))
def _vv10nlc(rho, coords, vvrho, vvweight, vvcoords, nlc_pars):
    rho = numpy.asarray(rho)
    return numint._vv10nlc(rho, coords, vvrho, vvweight, vvcoords, nlc_pars)

@_vv10nlc.defjvp
def _vv10nlc_jvp(coords, vvrho, vvweight, vvcoords, nlc_pars,
                 primals, tangents):
    rho, = primals
    rho_t, = tangents

    exc, vxc = _vv10nlc(rho, coords, vvrho, vvweight, vvcoords, nlc_pars)

    exc_jvp = (vxc[0] - exc) / rho[0] * rho_t[0] \
            + vxc[1] / rho[0] * 2. * jnp.einsum('np,np->p', rho[1:4], rho_t[1:4])
    # pylint: disable=W0511
    vxc_jvp = jnp.zeros_like(vxc) # FIXME gradient of vxc not implemented
    return (exc,vxc), (exc_jvp, vxc_jvp)

class NumInt(numint.NumInt):
    def _gen_rho_evaluator(self, mol, dms, hermi=0):
        if getattr(dms, 'mo_coeff', None) is not None:
            # pylint: disable=W0511
            #TODO: test whether dm.mo_coeff matching dm
            mo_coeff = dms.mo_coeff
            mo_occ = dms.mo_occ
            if isinstance(dms, numpy.ndarray) and dms.ndim == 2:
                mo_coeff = [mo_coeff]
                mo_occ = [mo_occ]
            nao = mo_coeff[0].shape[0]
            ndms = len(mo_occ)
            def make_rho(idm, ao, non0tab, xctype):
                return self.eval_rho2(mol, ao, mo_coeff[idm], mo_occ[idm],
                                      non0tab, xctype)
        else:
            if getattr(dms, "ndim", None) == 2:
                dms = [dms]
            if not hermi:
                # For eval_rho when xctype==GGA, which requires hermitian DMs
                dms = [(dm+dm.conj().T)*.5 for dm in dms]
            nao  = dms[0].shape[0]
            ndms = len(dms)
            def make_rho(idm, ao, non0tab, xctype):
                return self.eval_rho(mol, ao, dms[idm], non0tab, xctype, hermi=1)
        return make_rho, ndms, nao

    def eval_xc(self, xc_code, rho, spin=0, relativity=0, deriv=1, omega=None,
                verbose=None):
        if omega is None:
            omega = self.omega
        return libxc.eval_xc(xc_code, rho, spin, relativity, deriv,
                             omega, verbose)

    def eval_rho(self, mol, ao, dm, non0tab=None, xctype='LDA', hermi=0, verbose=None):
        return eval_rho(mol, ao, dm, non0tab, xctype, hermi, verbose)

    nr_rks = nr_rks
    nr_uks = nr_uks
