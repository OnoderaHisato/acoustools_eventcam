# Spherical-harmonic / Mie ARF hologram optimization for a 1cm sphere

Acoustic radiation force (ARF) computed via the spherical-harmonic / Mie
partial-wave method (Sapozhnikov-Bailey / Silva; NOT Inoue et al.'s
boundary-element method), used to optimize a phased-array hologram (Inoue
et al. 2019, Eq. 22) to levitate a 1cm-diameter, 24.8 kg/m^3 sphere with
acoustools' default two-sided 16x16 (512-element) 40kHz array.

## 粒子密度の修正（2026-09-10）

- 新規計算の粒子密度を **24.8 kg/m³** に修正しました。共通設定は
  `particle_parameters.py` の `PARTICLE_DENSITY_KG_M3` です。
  最適化3本、安定性確認2本、力プロファイル、Gor'kov比較検証が参照します。
- 直径10 mm・半径5 mm・重力加速度9.81 m/s²は変更しません。
  質量は **12.98525 mg**、目標上向き力 `mg` は **127.3853 µN** です。
  密度は重力だけでなくMie散乱係数にも使われます。
- **既存の結果JSON・NPY・図は40 kg/m³で計算した過去の成果物のままです。**
  以下の結果・精度記述も元の計算の記録で、新密度での再検証結果ではありません。
  `phase_acoustools_optimized_eps10mm_v3_xyz_complex64.npy` の位相も変更していません。
  新密度に最適化したNPYを得るには、最適化の再実行と力・安定性の再評価が必要です。
- `force_profile.py` は評価時の密度から重力を再計算し、出力の `meta` に評価条件、
  `source_optimization_meta` に元の最適化条件を分けて保存します。
  固定位相の入射音圧可視化は粒子密度を使わないため、密度変更だけでは変わりません。
- 今回は設定と軽量テストのみ更新し、最適化・力計算・実機送信は実行していません。
  現在のプロジェクトvenvにはSciPyがなく、SH/Mie計算には依存関係の準備も必要です。
  スクリプトは作業ディレクトリの固定名JSONへ保存するため、再計算時は別の新規作業
  ディレクトリを使い、必要な入力JSONをコピーして既存結果を保護してください。
  再最適化前に、既知の更新前の力評価／更新後の保存位相の1ステップ不一致も修正が必要です。

## Setup

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install acoustools numpy scipy matplotlib
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install vedo   # undeclared dependency of acoustools.Solvers
```
(acoustools needs Python >= 3.10; vedo is required by `acoustools.Solvers`
but isn't listed in its own dependencies.)

## Pipeline (run in this order)

1. **`arf_sh.py`** -- core physics module (numpy/scipy): spherical harmonics
   (`scipy.special.sph_harm_y`, convention-checked against closed forms),
   spherical Bessel/Hankel functions, beam-shape coefficients `S_nm` via the
   free-space multipole addition theorem (built directly from transducer
   positions + activations -- no field sampling/quadrature needed), Mie
   fluid-sphere scattering coefficients, and the force summation (a literal
   port of `Trust_Me_ARF/package_original/return_force.m` +
   `scattering_coefficient.m`, using our own validated `S_nm`/`Y_n^m`).
   Run directly (`python arf_sh.py`) to just execute the SH-convention
   self-check.

2. **`validate_pressure_reconstruction.py`** -- sanity check #1: does the
   `S_nm` addition-theorem field reconstruction match
   `acoustools.propagate()`? (Tests geometry/SH-convention only, no force
   physics.) Confirms <1% error at r=lambda/10, shrinking linearly with r
   (residual = the standard directivity-evaluated-at-center approximation).

3. **`validate_gorkov.py`** -- sanity check #2 (the user's requested r <
   lambda/10 validation): does the general SH/Mie force reduce to
   acoustools' own Gor'kov force as ka->0? Confirms agreement to ~0.5%,
   actually better than acoustools' own analytic-vs-finite-difference
   internal agreement (~1-2%).

4. **`arf_torch.py`** -- differentiable (PyTorch autograd) version of the
   same force computation, for use as an optimization objective.

5. **`optimize.py`** -- Eq. 22's force-matching term only, warm-started from
   a plain WGS focus, for weight vectors (0,0,1) and (1,1,1). Converges to
   Fz=mg almost exactly, but see `stability_check.py`: this solution is NOT
   a stable trap (pressure antinode, all-positive Jacobian eigenvalues).

6. **`optimize_v2.py`** + **`stability_check_v2.py`** -- same but warm-started
   from the standard twin-trap (pressure-node) signature. Better (axially
   stable) but still laterally unstable (1D-only trap).

7. **`optimize_v3_stable.py`** -- the complete Eq. 22 implementation: force
   term + the Lyapunov stability term `v_i*Re[lambda_i]`, computed via a
   differentiable finite-difference force-Jacobian (7 ARF model evaluations
   per step: center +/- delta along x,y,z). Finds a genuinely stable 3D trap
   (all 3 eigenvalues negative) while still matching Fz to ~0.07%.

8. **`force_profile.py`** + **`plot_force_profiles.py`** -- Fx(x,y=0,z=0),
   Fy(y,x=0,z=0), Fz(z,x=0,y=0) swept over +/-6mm for the converged v3
   solution.

9. **`pressure_field.py`** + **`plot_pressure_field.py`** -- XZ-plane
   pressure magnitude field (full cavity + zoomed around the sphere).

10. **`make_plots*.py`** -- assorted result figures (hologram phase pattern
    on both boards, force bar charts, stability eigenvalue bars).

## Historical results (1cm sphere, rho_p=40 kg/m^3, ka=3.664, n_max=10-14 converged)

- Target Fz = mg = 205.460 uN (r_a=5mm, V=5.236e-7 m^3)
- Force-matching-only (both weight vectors): Fz=205.46uN, Fx,Fy~1e-10N --
  but UNSTABLE (see caveat above)
- Full Eq. 22 (force + stability): Fz=205.60uN (~0.07% off target),
  Fx,Fy~nanonewton, all 3 stability eigenvalues negative (genuinely stable
  3D trap): Jxx=Jyy~=-0.032 N/m, Jzz~=-0.015 N/m

## Key implementation caveats (read before reusing)

- **c_p = 1052 m/s** (acoustools' own default "EPS particle" sound speed)
  was ASSUMED for the Mie scattering coefficients, since only density
  (originally 40 kg/m^3, now corrected to 24.8 kg/m^3) was specified.
  Rerun `arf_torch.py`'s `ARFModel(...)` call
  with a different `c_p` if you have a better value -- it matters at this
  ka (not deeply sub-wavelength).
- **Spherical harmonic convention**: `scipy.special.sph_harm_y(n, m,
  theta_polar, phi_azimuthal)` -- orthonormal, Condon-Shortley phase. The
  LEGACY `scipy.special.sph_harm(m, n, theta, phi)` has swapped argument
  order AND swapped angle meaning -- do not substitute one for the other.
- **Directivity is evaluated once per transducer at the sphere center**
  (not per field point) when building `S_nm` -- standard approximation in
  this class of method, validated to contribute <1% error at r=lambda/10.
- Torque is not modeled: Inoue et al. note (Sec. III A) that a true sphere
  carries zero controllable torque in their formulation, so Eq. 22 reduces
  to the 3 force components here.

`Trust_Me_ARF/` is the reference MATLAB implementation (Fushimi et al.) this
was cross-validated against; `arf_spherical_harmonics_theory.md` is a
from-scratch literature derivation/convention reference (Sapozhnikov-Bailey,
Silva et al.) used as a second independent cross-check.
