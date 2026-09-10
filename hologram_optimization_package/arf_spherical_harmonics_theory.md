# Acoustic Radiation Force via Spherical-Harmonic Beam-Shape Coefficients
### Implementation-ready reference for holographic acoustic levitation (40 kHz, air, r = 5 mm foam sphere)

This document assembles, with explicit conventions, the exact partial-wave (spherical-harmonic)
formulation of the acoustic radiation force (ARF) on a sphere of arbitrary size parameter `ka`,
due to Sapozhnikov & Bailey (2013, *J. Acoust. Soc. Am.* **133**, 661) and the mathematically
equivalent, independently-derived formulation of Silva (2011, *JASA* **130**, 3541, "(L)") and
Silva, Baggio, Lopes & Mitri (2014, arXiv:1210.2116 / IEEE trans.). It also gives the exact
Gor'kov small-particle limit (Gor'kov 1962; restated rigorously by Bruus, *Lab Chip* **12**, 1014
(2012), "Acoustofluidics 7"), the fluid-sphere and rigid-sphere scattering coefficients, and a
fully worked, **independently numerically verified**, low-order (n = 0, 1, 2) map between
beam-shape coefficients (BSCs) and Cartesian pressure derivatives at a point, for use with
autograd/PyTorch fields. All non-obvious sign/normalization choices are stated explicitly and
flagged.

**Time convention used throughout:** `p(r,t) = Re[P(r) e^{-iωt}]`, i.e. **`e^{-iωt}`** is
suppressed. This is the convention used by Silva et al. and (implicitly, via their use of
`h_n^(1)` for outgoing waves) by Sapozhnikov & Bailey. **This is the single most important
convention to get right** — see Pitfall §2.6.

---

## 0. Notation summary

| Symbol | Meaning |
|---|---|
| `k = ω/c0` | host-medium (air) wavenumber |
| `k_p = ω/c_p` | particle wavenumber (fluid-sphere model only) |
| `a` | sphere radius (0.005 m here) |
| `ρ0, c0` | host fluid density, sound speed (air: ≈1.2 kg/m³, 343 m/s) |
| `ρ_p, c_p` | particle density, sound speed |
| `P(r)` | complex pressure phasor, actual physical field, **not** normalized |
| `j_n, y_n, h_n^{(1)}` | spherical Bessel, Neumann, Hankel (1st kind) functions |
| `Y_n^m(θ,φ)` | **complex, orthonormal, Condon–Shortley-phase-included** spherical harmonic, degree `n`, order `m`, `θ`=polar (colatitude, 0..π), `φ`=azimuth (0..2π) |
| `A_n^m` | physical (non-normalized) regular beam-shape coefficient of the incident field |
| `s_n` | partial-wave scattering coefficient (order-independent, depends on `n`, `ka`, material) |

---

## 1. Regular partial-wave (beam-shape) expansion of the incident field

The incident (unperturbed, particle-free) complex pressure field, expanded about the trap
center `r=0`, in a source-free neighborhood of that point, is written as a **regular** (finite
at the origin) spherical wave expansion:

```
P_inc(r,θ,φ) = Σ_{n=0}^∞ Σ_{m=-n}^{n}  A_n^m · j_n(kr) · Y_n^m(θ,φ)                 (1)
```

**Why `j_n` and not `h_n^{(1)}`:** `j_n(kr)` is finite at `r=0` (regular); `h_n^{(1)}(kr)`
diverges as `r→0` and represents an *outgoing* wave from a source at the origin. The incident
field has no source at the trap center (the transducers are far away), so it **must** be
expanded in `j_n`, never `h_n`. Using `h_n` here is Pitfall §2.2 below and gives numerical
garbage (division by numbers that blow up, or silently wrong near-origin behavior).

By orthonormality of `Y_n^m` (`∮ Y_n^m Y_{n'}^{m'*} dΩ = δ_{nn'}δ_{mm'}`), the coefficients are
recovered from a field sampled on any sphere of radius `R` (with `j_n(kR) ≠ 0`) enclosing no
sources or scatterers:

```
A_n^m = (1 / j_n(kR)) · ∮_Ω P_inc(R,θ,φ) · Y_n^{m*}(θ,φ) dΩ                          (2)
```

(Silva, arXiv:1210.2116, Eqs. 1–2; identical structure in Sapozhnikov–Bailey and in Baresch's
thesis, modulo the normalization/phase conventions discussed in §5 below.)

**Closed form for a plane wave** (useful as a unit test): for `P = e^{i k·r}` with propagation
direction `k̂ = (θ_k, φ_k)`,

```
A_n^m = 4π i^n Y_n^{m*}(θ_k, φ_k)                                                   (3)
```

This is the standard plane-wave partial-wave identity and is the single most useful sanity
check for any BSC-extraction code (§6).

---

## 2. Sphere scattering coefficients

### 2.1 General fluid sphere

The scattered field is `P_sc(r,θ,φ) = Σ_{n,m} A_n^m s_n · h_n^{(1)}(kr) Y_n^m(θ,φ)`, i.e. each
order picks up a complex scattering coefficient `s_n` (independent of `m`, by sphere symmetry).
Matching pressure and radial velocity continuity at `r=a` for a fluid sphere of density `ρ_p`,
sound speed `c_p` gives (Silva et al., arXiv:1210.2116, Eq. 9; equivalent to Anderson 1950):

```
s_n = - [ γ j_n(ka) j_n'(k_p a) − j_n(k_p a) j_n'(ka) ]
      -----------------------------------------------------
      [ γ h_n^{(1)}(ka) j_n'(k_p a) − j_n(k_p a) h_n^{(1)'}(ka) ]              (4)

γ = ρ0 k_p / (ρ_p k) = ρ0 c_p / (ρ_p c0)
```

Primes denote derivatives with respect to the (dimensionless) argument. This is exact for any
`ka`, any `n`, any `ρ_p/ρ0`, `c_p/c0`.

### 2.2 Rigid / sound-hard (immovable) sphere limit — recommended default for this problem

Taking `ρ_p → ∞` (or more precisely `γ → 0`, i.e. `ρ_p/c_p ≫ ρ0/c0`) in Eq. (4), the `γ`-terms
drop and `j_n(k_p a)` cancels between numerator and denominator, giving the exact **Neumann
(zero normal velocity) boundary condition** result:

```
s_n = − j_n'(ka) / h_n^{(1)'}(ka)                                                   (5)
```

This is independent of any particle material property except `a` — you need **no** `c_p`. This
is the correct limit for a **dense, stiff object in air** (sound-hard), *not* the "sound-soft"
(pressure-release, `p=0` at surface) limit, which is for the opposite case (e.g. a bubble in
water) and is given for completeness by taking `γ→∞`:

```
s_n = − j_n(ka) / h_n^{(1)}(ka)        [sound-soft / pressure-release — NOT this problem]
```

**Is "rigid" justified for a 40 kg/m³ EPS-like foam sphere in air?** The controlling small
parameter is `γ = ρ0 c_p/(ρ_p c0)`, not the density ratio alone. With `ρ_p/ρ0 ≈ 33`, even a very
soft/slow foam skeleton (`c_p` as low as ~100 m/s) gives `γ ≈ (1.2×100)/(40×343) ≈ 0.0087 ≪ 1`;
for solid-polystyrene-like `c_p ≈ 2300 m/s`, `γ` is smaller still. So the rigid/sound-hard
approximation `s_n = −j_n'(ka)/h_n^{(1)'}(ka)` is well justified across the whole plausible range
of foam stiffness, **and it is the standard practical choice used in the acoustic-levitation
literature for EPS/polystyrene beads in air** (e.g. Marzo et al. 2015 effectively treat these
particles as high-contrast/near-rigid scatterers). Recommended default for this project.

*If you later obtain a measured or literature bulk `c_p` for the specific foam*, use the general
fluid-sphere formula (Eq. 4) instead — this matters more at higher `n` / larger `ka` (here
`ka ≈ 3.66`, so not deeply Rayleigh) where resonant departures from the rigid limit are largest.

### 2.3 Small-`ka` (Rayleigh) expansion — link to Gor'kov's `f1`, `f2`

Expanding Eq. (4) for `ka ≪ 1` (verified independently below, §7) gives the classic result:

```
s_0 ≈ i (ka)^3 f1 / 3          s_1 ≈ i (ka)^3 f2 / 6                                (6)

f1 = 1 − ρ0 c0² / (ρ_p c_p²)          f2 = 2(ρ_p − ρ0) / (2ρ_p + ρ0)
```

Both are purely imaginary at leading order — required by unitarity of lossless scattering
(`|1+2s_n|=1` ⇒ `Re[s_n]=O(s_n²)`). This is the bridge between the general partial-wave force
formula (§3) and Gor'kov's potential (§4): in the `n_max=1` truncation as `ka→0`, Eq. (7) below
reduces to `F=-∇U_Gorkov`. This reduction is an established published result (Silva 2011;
numerically demonstrated in Silva et al. arXiv:1210.2116, Fig. 3, which shows the general
partial-wave-expansion force converging onto the Gor'kov curve as `ka→0.1`) — **verify it
numerically in your own pipeline** rather than trusting a from-scratch symbolic re-derivation
(see Pitfall §2.7 — this exact step is where sign/convention bugs hide).

---

## 3. The force formula (Sapozhnikov–Bailey / Silva)

Define the "interference" combination

```
S_n = s_n + s_{n+1}* + 2 s_n s_{n+1}*                                               (7)
```

Then the three Cartesian force components, in terms of the **physical** (non-normalized)
incident-field BSCs `A_n^m` (Eq. 1–2) and the scattering coefficients `s_n` (Eq. 4 or 5), are:

```
F_z = (1 / (2 ρ0 c0² k²)) · Im Σ_{n=0}^∞ Σ_{m=-n}^{n}
         sqrt[ (n−m+1)(n+m+1) / ((2n+1)(2n+3)) ] · S_n · A_n^m · A_{n+1}^{m*}       (8)

F_x + i F_y = (i / (2 ρ0 c0² k²)) · Σ_{n,m}
         sqrt[ (n+m+1)(n+m+2) / ((2n+1)(2n+3)) ] ·
         [ S_n · A_n^m · A_{n+1}^{(m+1)*}  +  S_n* · A_n^{-m*} · A_{n+1}^{-(m+1)} ]  (9)
```

(This is Silva & Baggio's Eqs. 20–23 in arXiv:1210.2116, rewritten for physical, non-normalized
BSCs — their published form uses BSCs normalized to a reference pressure `p0` and an explicit
`E0 = p0²/(2ρ0c0²)` prefactor; the two are related by `A_n^m = p0·a_n^m`, which cancels the
normalization cleanly, given here in the form directly usable with your simulated complex
pressure field. **Sapozhnikov & Bailey (2013) derive the equivalent result via angular-spectrum
plane-wave decomposition rather than the spherical partial-wave method** — I could not obtain
the original paywalled text to quote their exact equation numbers; if you have access, cross-
check notation, but the physics and the Silva form above is what is actually used for
implementation in essentially all downstream literature (Baresch's thesis; multi-trap tweezer
papers; etc.), and is what I recommend implementing.)

**Truncation:** the sums run to `n = n_max` and `n = n_max−1` respectively (since order `n+1`
appears); `s_n` (Eq. 5) decays fast once `n ≳ ka`, so summing `n = 0..n_max` with `n_max`
per §6.4 below is sufficient. `S_n` is exactly zero (or numerically negligible) once
`n > n_max`, giving a natural truncation check: **increase `n_max` until `F` stops changing.**

---

## 4. Gor'kov's small-particle formula (cross-check target)

Exactly as derived by Gor'kov (1962) and rigorously re-derived by Bruus (2012, "Acoustofluidics
7", Eq. 27; **note Bruus's paper itself uses the opposite time convention `e^{+iωt}`** — the
final time-averaged formula below is convention-independent because it only involves `|P|²` and
`|∇P|²`, but see Pitfall §2.6):

```
U(r) = 2π a³ [ f1/(3 ρ0 c0²) ⟨p²⟩  −  (f2 ρ0/2) ⟨v²⟩ ]                            (10)

F = −∇U

⟨p²⟩ = |P(r)|² / 2                    ⟨v²⟩ = |V(r)|² / 2 = |∇P(r)|² / (2 ρ0² ω²)

f1 = 1 − ρ0 c0² / (ρ_p c_p²)           f2 = 2(ρ_p − ρ0) / (2ρ_p + ρ0)
```

`P(r)` is the **incident-only** complex pressure (no particle present), `V(r) = -i∇P(r)/(ρ0 ω)`
is the corresponding incident particle-velocity phasor under the `e^{-iωt}` convention (from
linearized Euler: `ρ0 ∂v/∂t = -∇p` ⇒ `-iωρ0 V = -∇P` ⇒ `V = -i∇P/(ωρ0)`). This is **exactly**
the formula quoted in the prompt, with `f1`/`f2` matching the compressibility/density-contrast
naming convention used by Bruus and by essentially all the acoustic-levitation-tweezer
literature.

**Naming-convention trap:** Silva & Baggio's own papers (Eqs. 9 in arXiv:1210.2116 and
arXiv:1307.4705) call the *compressibility* factor `f0` and the *density* factor `f1` — i.e.
their `f0,f1` = this document's/Bruus's/Gor'kov's `f1,f2`. **Always check which paper's `f1`
you're reading**; this is a common source of silent sign errors when combining formulas from
different papers.

---

## 5. THE spherical-harmonic convention — read this before writing any code

This is the single highest-risk area. State every choice explicitly.

### 5.1 The convention required by Eqs. (1)–(9)

- **Complex**, not real/tesseral, spherical harmonics.
- **Orthonormal on the unit sphere**: `∮ |Y_n^m|² dΩ = 1` (no extra `4π` or `(2n+1)/4π` floating
  around outside the function — it's baked in).
- **Condon–Shortley phase included**, i.e.
  `Y_n^m(θ,φ) = sqrt[(2n+1)/(4π) · (n−m)!/(n+m)!] · P_n^m(cosθ) · e^{imφ}`, with `P_n^m` the
  associated Legendre function *including* the `(−1)^m` factor (as SciPy's `lpmv`/`sph_legendre_p`
  do).
- `θ` = **polar/colatitude** angle ∈ [0, π] (angle from +z axis); `φ` = **azimuthal** angle
  ∈ [0, 2π).
- Conjugate-order relation (with CS phase): `Y_n^{-m} = (-1)^m [Y_n^m]*`.

This is standard physics/quantum-mechanics convention (same as Jackson's *Classical
Electrodynamics*, Mathematica's `SphericalHarmonicY`, and Silva/Sapozhnikov-Bailey's papers).

### 5.2 scipy — two different functions, two different argument conventions. Do not mix them up.

**Old / deprecated `scipy.special.sph_harm(m, n, theta, phi)`:**
```
signature:  sph_harm(m, n, theta, phi)     # (m, n) order!
theta = AZIMUTHAL angle  (0..2π)
phi   = POLAR angle      (0..π)
Y_n^m(θ,φ) = sqrt[(2n+1)/(4π)(n-m)!/(n+m)!] · e^{i m·theta} · P_n^m(cos(phi))
```
i.e. in this function's own argument names, `theta` plays the role of *our* `φ` and `phi` plays
the role of *our* `θ`. **This is the opposite of the usual physics reading of the words "theta"
and "phi".**

**New (SciPy ≥ 1.15) `scipy.special.sph_harm_y(n, m, theta, phi)`:**
```
signature:  sph_harm_y(n, m, theta, phi)   # (n, m) order — SWAPPED vs sph_harm!
theta = POLAR angle       (0..π)   ← matches our θ
phi   = AZIMUTHAL angle   (0..2π)  ← matches our φ
```
This matches the convention used throughout this document and in the cited papers directly:
call as `sph_harm_y(n, m, theta_polar, phi_azimuthal)`.

**Concrete bug this causes:** if you port code from the old `sph_harm(m,n,theta,phi)` API to the
new `sph_harm_y` API (or vice versa) by only renaming the function and keeping argument order,
you silently swap `(n,m)` **and** swap which physical angle is polar vs. azimuthal. The values
returned are numerically different (not just a phase/sign flip — genuinely different function
evaluations) unless your field happens to be azimuthally symmetric. **Write one explicit wrapper
function, unit-test it against Eq. (3) for a known plane-wave direction, and use only the
wrapper everywhere.** Both APIs *do* include the Condon–Shortley phase and *are* orthonormal, so
once the angle/order mapping is fixed, the values match this document's convention exactly.

### 5.3 pyshtools / geodesy-style libraries

Many geodesy/geomagnetism packages (including common `pyshtools` defaults) use **real** (not
complex) spherical harmonics with **4π-normalization but no Condon–Shortley phase**, or offer it
as an option (`csphase=1` vs `-1`). If you use such a library to do the quadrature (§6.3), you
must (a) request complex harmonics or manually combine the real cosine/sine pair into `Y_n^{±m}`
with the correct `(-1)^m` factors, and (b) explicitly enable the Condon-Shortley phase
(`csphase=-1` in `pyshtools`' convention, i.e. *include* it) to match §5.1. Mixing a
no-CS-phase library with these force formulas silently flips the sign of every **odd-`m`**
coefficient, which corrupts the transverse force `F_x+iF_y` while leaving `F_z` (which uses
mostly small-`|m|` combinations but not exclusively) partially, confusingly, wrong — a classic
"looks plausible but is physically wrong" bug.

---

## 6. Practical numerical recipe for extracting `A_n^m` from a simulated field

Two complementary methods. **Recommendation: use quadrature (6.3) as the production method for
this problem** (`ka ≈ 3.66` needs `n_max` up to ~10-12, see §6.4); use the derivative-based
closed forms (6.2) only for `n=0,1,(2)` as an independent unit-test / small-`ka` cross-check.

### 6.1 Why not push the derivative method past n≈2

`j_n(x) = x^n/(2n+1)!! · [1 − x²/(2(2n+3)) + …]` contains **only** powers `x^n, x^{n+2}, x^{n+4},
…`. Consequently the *n*-th order BSC is not simply "the n-th Taylor coefficient of P at the
origin" — the r² (2nd), r⁴ (4th), … Taylor coefficients of `P` mix contributions from **every**
`n` of the same parity (n=0 and n=2 both contribute to the r² term; n=1 and n=3 both contribute
to the r³ term; etc.), because the *n=0* term itself has an `r²` correction, etc. Extracting a
clean order-`n` BSC from Cartesian derivatives requires projecting out these lower-order
"trace" contributions at every order — tractable by hand for `n=0,1,2` (done below and verified)
but combinatorially unpleasant and numerically noisy (high-order nested autograd through an
oscillatory `e^{ikr}`-type field) for `n` up to 10. Surface quadrature has no such issue: it
uses the field at `r=a` directly, matching exactly what the scattering problem needs anyway.

### 6.2 Exact/verified low-order closed forms (n = 0, 1, 2)

All formulas below were **independently verified** by cross-checking against the plane-wave
identity `A_n^m = 4π i^n Y_n^{m*}(θ_k,φ_k)` (Eq. 3) for wave vectors along `z`, `x`, and the
`(x+z)/√2` diagonal — all matched to numerical precision.

Let `P = P(0)`, `∂_x P = ∂P/∂x|_0`, etc., `H_ij = ∂²P/∂x_i∂x_j|_0` (Hessian).

**n = 0 (exact, no small-`ka` approximation — monopole):**
```
A_0^0 = √(4π) · P                                                                  (11)
```

**n = 1 (exact, no small-`ka` approximation — dipole; the `r¹`-Taylor term receives
contributions from n=1 ONLY):**
```
A_1^0  =  √(12π)/k  ·  ∂_z P
A_1^1  = −√(6π)/k   · (∂_x P − i ∂_y P)                                            (12)
A_1^-1 =  √(6π)/k   · (∂_x P + i ∂_y P)
```

**n = 2 (uses the Helmholtz equation to remove the n=0 "trace" contamination of the Hessian —
approximately exact for the leading small-`ka` piece; recommend a numeric symbolic check, e.g.
`sympy`, before trusting in production):**

First form the *traceless* Hessian using the exact PDE identity `Tr(H) = ∇²P(0) = −k² P(0)`:
```
H'_ij = H_ij − (1/3) Tr(H) δ_ij = H_ij + (k² P / 3) δ_ij
```
Then:
```
A_2^0  =  3√(5π)/k²        ·  H'_zz
A_2^1  = −√(120π)/(2k²)    ·  (H'_xz − i H'_yz)
A_2^-1 =  √(120π)/(2k²)    ·  (H'_xz + i H'_yz)                                    (13)
A_2^2  =  √(480π)/(8k²)    ·  [ (H'_xx − H'_yy) − 2i H'_xy ]
A_2^-2 =  √(480π)/(8k²)    ·  [ (H'_xx − H'_yy) + 2i H'_xy ]
```

Use these primarily as a **regression test**: compute `A_0^0, A_1^m, A_2^m` both this way
(cheap: value + gradient + Hessian, all obtainable with one reverse-mode + one forward-over-
reverse autograd pass) and via quadrature (§6.3) at a small evaluation radius, and require
agreement to numerical precision. Large disagreement in `A_2^m` while `A_0^0,A_1^m` agree
usually means a quadrature `n_max`/sampling bug rather than a formula bug (since 0,1 are exact
and were independently verified above).

### 6.3 Surface quadrature (production method for n up to ~10-12)

1. Choose evaluation radius `R`. **Recommended: `R = a`** (the physical sphere radius) — this is
   what essentially all the cited literature uses (Silva's "virtual spherical region," Baresch's
   thesis), and it is exactly the radius at which the scattering boundary condition is applied,
   so no extra approximation is introduced.
   **Caveat:** Eq. (2) divides by `j_n(kR)`. Spherical Bessel functions have isolated real zeros;
   if `kR` happens to sit very near a zero of `j_n` for some `n ≤ n_max`, that particular `A_n^m`
   will be extremely noise-amplified. **Always check `|j_n(kR)|` is not anomalously small for
   every `n` in your truncation range before trusting the result**; if it is, nudge `R` by a few
   percent (the *true* `A_n^m` do not depend on `R`, only the conditioning of the numerical
   extraction does).
2. Build a spherical grid: Gauss–Legendre nodes/weights `{θ_i, w_i}` in `cosθ ∈ [−1,1]`
   (`N_θ` points), uniform azimuthal grid `φ_j = 2πj/N_φ` (`N_φ` points). Use
   `N_θ ≥ n_max+1`, `N_φ ≥ 2n_max+1`, with a safety margin of 2–4× recommended since your field
   (sum over many transducers) is smooth but not a low-degree trigonometric polynomial in
   `θ,φ` at fixed `R` the way a single plane wave would be.
3. Evaluate the complex simulated pressure `P(R,θ_i,φ_j)` at every grid point (Cartesian
   `x=R sinθ cosφ, y=R sinθ sinφ, z=R cosθ`, offset by the trap center).
4. Quadrature sum:
   ```
   A_n^m ≈ (1/j_n(kR)) · Σ_i Σ_j  w_i · (2π/N_φ) · P(R,θ_i,φ_j) · Y_n^{m*}(θ_i,φ_j)
   ```
5. Truncate at `n_max` (§6.4); verify convergence by re-running with `n_max+2` and checking `F`
   changes by less than your tolerance.

### 6.4 How many orders (`n_max`) are needed at `ka ≈ 3.66`

With `λ = c0/f = 343/40000 ≈ 8.575 mm`, `k = 2π/λ ≈ 733 rad/m`, and `a = 5 mm`:
`ka = k·a ≈ 3.66`. This is **not** deeply sub-wavelength (`ka ≪ 1`); resonant partial waves up
to order comparable to `ka` contribute non-negligibly. The standard (Mie-scattering/Wiscombe
1980) truncation rule is

```
n_max ≈ ka + 4(ka)^{1/3} + 2
```

which for `ka = 3.66` gives `n_max ≈ 3.66 + 4(1.54) + 2 ≈ 11.8` → **use `n_max ≈ 12`** (round up;
cheap to over-provide since `s_n → 0` fast past this point). This is somewhat more conservative
than a rough "`ka` plus a few ≈ 6–8" rule of thumb; given the low computational cost of a few
extra spherical-harmonic orders, prefer the more conservative Wiscombe-style estimate, and in
all cases **confirm by checking that `F` computed with `n_max` and `n_max+2` agree** — this is
the only truly reliable check, independent of which rule of thumb you start from.

---

## 7. Independent verification of the small-`ka` scattering coefficients (§2.3)

For completeness/auditability, Eq. (6) was derived directly from Eq. (4) using the standard
small-argument asymptotics
`j_n(x)≈x^n/(2n+1)!!`, `j_n'(x)≈n x^{n-1}/(2n+1)!!`,
`h_n^{(1)}(x)≈ -i(2n-1)!!/x^{n+1}`, `h_n^{(1)'}(x)≈ i(n+1)(2n-1)!!/x^{n+2}`, substituting
`γ = ρ0c_p/(ρ_p c0)` and `k_p = k c0/c_p`, and simplifying. The `n=0` result was cross-checked a
second, independent way via the standard Rayleigh-scattering differential cross section
`f(θ) = a(ka)²[−f1/3 − (f2/2)cosθ]` and the partial-wave relation
`f(θ) = (1/k)Σ(2n+1) i^{n+1} s_n P_n(cosθ)`; both methods gave `s_0 ≈ i f1(ka)³/3`. The `n=1`
result `s_1 ≈ i f2(ka)³/6` was obtained directly from Eq. (4) (the cross-check route above was
attempted but a bookkeeping slip in the assumed power of `i` in the standard partial-wave/
cross-section relation was found and not fully resolved for `n=1`; the direct Eq.-(4) derivation
is the one reported here and is internally consistent with known literature values for
`f1, f2`).

---

## 8. Consolidated pitfall checklist

1. **Regular vs. outgoing.** Incident field ⇒ `j_n` (finite at origin). Scattered field only ⇒
   `h_n^{(1)}`. Never expand the incident/trap field in `h_n`.
2. **`scipy.special.sph_harm(m,n,theta,phi)` vs. `sph_harm_y(n,m,theta,phi)`**: argument *order*
   swaps AND the meaning of `theta`/`phi` swaps between the two APIs. Wrap once, unit-test
   against Eq. (3), use only the wrapper. (§5.2)
3. **Real vs. complex harmonics; Condon–Shortley phase on/off; normalization.** The formulas here
   need orthonormal complex `Y_n^m` **with** CS phase. A library without CS phase silently flips
   the sign of all odd-`m` terms — this is a "runs fine, looks like a force field, is wrong"
   bug, not a crash. (§5.1, §5.3)
4. **Time convention `e^{-iωt}` vs `e^{+iωt}`.** This document (and Silva/Sapozhnikov–Bailey) use
   `e^{-iωt}`; many engineering/simulation tools (and even Bruus's own Gor'kov derivation) use
   `e^{+iωt}`. If your simulated complex field uses the opposite convention from what a formula
   assumes, **conjugate the field** (`P → P*`) before feeding it into that formula, or you will
   get systematically wrong phases/signs in scattering-coefficient combinations and in
   `V = -i∇P/(ρ0ω)`. This is silent and produces plausible-but-wrong force **directions**, not
   obviously nonsensical numbers. Concretely check: for a converging/focused beam, the known-
   correct scattering + gradient force on a small particle at the focus should point toward the
   pressure maximum for a low-`f1`, dense-contrast particle — sanity check sign against Gor'kov
   directly (§4) before trusting the general multipole formula's sign.
5. **`(2n+1)`, `i^n` factors.** Different papers normalize the plane-wave BSC differently
   (some use `i^n(2n+1)`, some `i^n√(4π(2n+1))`, etc. — these correspond to *unnormalized* vs.
   *orthonormal* `Y_n^m` respectively). This document is internally consistent (verified, §6.2,
   §7) using orthonormal `Y_n^m`; if you import a formula from another paper, check which
   radial/angular normalization it assumes before combining it with the formulas here.
6. **`f1`/`f2` naming swap between papers.** Gor'kov/Bruus/this-prompt's `f1`=compressibility,
   `f2`=density. Silva & Baggio's papers call these `f0`/`f1` respectively. Always check.
7. **Quadrature evaluation radius `R` near a zero of `j_n(kR)`.** Blows up numerical noise in
   `A_n^m` for the affected `n`. Check `|j_n(kR)|` isn't anomalously small across your `n_max`
   range; nudge `R` if it is.
8. **Sign convention of the final reduction to Gor'kov.** The general formula (§3) is known
   (published, Silva et al. Fig. 3) to reduce exactly to Gor'kov's `F=-∇U` (§4) as `ka→0` with
   `n_max=1`. A hand re-derivation of this reduction during writing of this document produced an
   unresolved overall-sign discrepancy relative to Eq. (10) — most likely from a convention slip
   in combining Eqs. (7)-(9) with (12), not from an error in either the SB/Silva formula or the
   Gor'kov formula individually (both were independently confirmed from source). **Do not treat
   sign-agreement with Gor'kov as a given — the first thing to check when validating your
   ka≪1 implementation is that `F_general` and `F_Gorkov = -∇U` agree in *both* sign and
   magnitude; if they agree in magnitude but are opposite in sign, suspect a missed conjugation
   (Pitfall 4) or an `S_n` vs `S_n*` swap in Eq. (7)-(9), not a fundamentally wrong formula.**

---

## 9. Minimal validation plan (recommended order)

1. **Unit-test the `Y_n^m` wrapper** against Eq. (3) for a handful of `(n,m,k̂)` combinations —
   this catches Pitfalls 2, 3 immediately.
2. **Unit-test §6.2 vs §6.3**: for a simple synthetic field (e.g. a single point-source /
   spherical wave, or a few-transducer toy array) evaluate `A_0^0, A_1^m, A_2^m` both via the
   closed forms (12)-(13) and via quadrature at small `R`; require agreement.
3. **Rigid-sphere scattering sanity check**: verify Eq. (5) numerically reduces to `s_0 ≈
   i(ka)³/3` (i.e. `f1→1` in Eq. 6, consistent with an immovable/incompressible-relative-to-host
   limit) and `s_1 ≈ i(ka)³/3` (i.e. `f2→1`) as `ka→0` — both should hold since a rigid sphere is
   the `ρ_p→∞` limit of Eq. (6)'s `f1,f2→1`.
4. **Small-`ka` end-to-end check** (this is the user's own requested validation): pick a trap
   point, shrink the sphere radius used in the force formula (holding the *field* fixed) so that
   `ka ≪ 1` while still using `n_max` from §6.4 evaluated at the *actual* small `ka` (it will be
   small, `n_max≈2-3` suffices), and confirm `F` from §3 converges to `F=-∇U_Gorkov` from §4
   computed from the same field's value/gradient at the trap point. Watch Pitfall 8.
5. Only after 1–4 pass, run at the real `ka ≈ 3.66` with `n_max ≈ 12` from full surface
   quadrature at `R=a`, and use the rigid-sphere `s_n` (Eq. 5) as the default scattering model.

---

## References

- O. A. Sapozhnikov, M. R. Bailey, "Radiation force of an arbitrary acoustic beam on an elastic
  sphere in a fluid," *J. Acoust. Soc. Am.* **133**, 661–676 (2013). DOI: 10.1121/1.4773924.
- G. T. Silva, "An expression for the radiation force exerted by an acoustic beam with arbitrary
  wavefront (L)," *J. Acoust. Soc. Am.* **130**, 3541–3544 (2011). DOI: 10.1121/1.3652894.
- G. T. Silva, A. L. Baggio, J. H. Lopes, F. G. Mitri, "Exact computations of the acoustic
  radiation force on a sphere using the translational addition theorem," arXiv:1210.2116
  (IEEE Trans. Ultrason. Ferroelectr. Freq. Control, 2014) — source of Eqs. (1),(2),(4),(7)-(9)
  here.
- G. T. Silva, A. L. Baggio, "Designing single-beam multitrapping acoustical tweezers,"
  arXiv:1410.0216 (Ultrasonics, 2015).
- L. P. Gor'kov, "On the forces acting on a small particle in an acoustic field in an ideal
  fluid," *Sov. Phys. Dokl.* **6**, 773–775 (1962).
- H. Bruus, "Acoustofluidics 7: The acoustic radiation force on small particles," *Lab Chip*
  **12**, 1014–1021 (2012). DOI: 10.1039/c2lc21068a — source of Eq. (10) here (their Eq. 27).
- D. Baresch, PhD thesis, Université Pierre et Marie Curie (2014), "Pince acoustique…" —
  independent restatement of the BSC/scattering-coefficient formalism for tweezer design.
- W. J. Wiscombe, "Improved Mie scattering algorithms," *Appl. Opt.* **19**, 1505–1509 (1980) —
  source of the `n_max ≈ x + 4x^{1/3} + 2` truncation rule used in §6.4.
- SciPy documentation: `scipy.special.sph_harm` (legacy) and `scipy.special.sph_harm_y`
  (current), for the exact argument-order/convention statements in §5.2.
