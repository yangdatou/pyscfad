from pyscfad.pbc import gto
from pyscfad.pbc import scf, df

basis = 'gth-szv'
pseudo = 'gth-pade'

a = 5.431020511
lattice = [[0., a/2, a/2],
          [a/2, 0., a/2],
          [a/2, a/2, 0.]]
disp = 0.01
atom = [['Si', [0., 0., 0.]],
        ['Si', [a/4+disp, a/4+disp, a/4+disp]]]

cell = gto.Cell()
cell.atom = atom
cell.a = lattice
cell.basis = basis
cell.pseudo = pseudo
cell.build()
kpts = cell.make_kpts([2,1,1])

mf = scf.KRHF(cell, kpts=kpts, exxdiv=None)
mf.kernel()
jac = mf.energy_grad()
print(jac.coords)
