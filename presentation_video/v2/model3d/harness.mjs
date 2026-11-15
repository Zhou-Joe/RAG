// Renders the app's own Seven Dwarfs Mine Train model: whole train -> C1 left-front C-frame.
import * as THREE from 'three';
import {VehicleModel} from '/static/vehicle_tracker/js/vehicle-tracker-model.mjs';
import {PART_MAP, within} from '/static/vehicle_tracker/js/vehicle-tracker-core.mjs';

const N = 240;
const SEL = 'C1-LF-CF', CARRIER = 'C1-LF';
const SHELL = new Set(['body', 'chassis', 'frame', 'towbar', 'dog']);
const HL = new THREE.Color(0x3ed3b4);

const clamp = (v) => Math.max(0, Math.min(1, v));
const smooth = (v) => { v = clamp(v); return v * v * (3 - 2 * v); };
const easeInOut = (v) => { v = clamp(v); return v < .5 ? 4 * v ** 3 : 1 - (-2 * v + 2) ** 3 / 2; };

async function post(path, body, type) {
  const r = await fetch(path, {method: 'POST', body, headers: {'Content-Type': type}});
  if (!r.ok) throw new Error(path + ' ' + r.status);
}

async function main() {
  const host = document.getElementById('host');
  const m = new VehicleModel(host, {select() {}, hover() {}, xrayChanged() {}, focusRequest() {}});
  m.requestRender = () => {};
  if (m.frame) cancelAnimationFrame(m.frame);
  m.resize();

  m.orientation('perspective', {animate: false});
  const A = {pos: m.camera.position.clone(), tgt: m.controls.target.clone()};
  m.focus(SEL, {animate: false});
  const B = {pos: m.camera.position.clone(), tgt: m.controls.target.clone()};
  m.xray = false;
  m.outline.visible = false;

  const cfBox = new THREE.Box3().setFromObject(m.groups.get(SEL));
  const cfCenter = cfBox.getCenter(new THREE.Vector3());
  const canvas = m.renderer.domElement;
  const meta = [];

  // camera = target + direction * distance; distance zooms on a log scale so the camera never cuts through a coach
  const dirA = A.pos.clone().sub(A.tgt), dirB = B.pos.clone().sub(B.tgt);
  const logA = Math.log(dirA.length() * .78), logB = Math.log(dirB.length() * 1.05);
  dirA.normalize(); dirB.normalize();
  const turn = new THREE.Quaternion().setFromUnitVectors(dirA, dirB);

  for (let f = 0; f < N; f++) {
    const u = f / (N - 1);
    const st = easeInOut((u - .04) / .5), sd = easeInOut((u - .1) / .55), sz = easeInOut((u - .08) / .64);
    const kx = smooth((u - .22) / .2), kh = smooth((u - .58) / .14), kg = smooth((u - .74) / .16);
    const target = A.tgt.clone().lerp(B.tgt, st);
    const dir = dirA.clone().applyQuaternion(new THREE.Quaternion().slerp(turn, sd));
    const dist = Math.exp(logA + (logB - logA) * sz);
    // gentle orbit: drift at the start, settle round the part at the end
    const ang = -.12 * (1 - smooth(u / .3)) + .32 * smooth((u - .68) / .32);
    const rel = dir.multiplyScalar(dist).applyAxisAngle(new THREE.Vector3(0, 1, 0), ang);
    m.camera.position.copy(target).add(rel);
    m.controls.target.copy(target);
    m.camera.lookAt(target);
    m.camera.updateMatrixWorld();

    for (const mesh of m.meshes) {
      const id = mesh.userData.partId, type = PART_MAP.get(id)?.type, mat = mesh.material;
      mat.color.copy(mesh.userData.baseColor);
      const inCarrier = within(id, CARRIER), isSel = within(id, SEL);
      if (isSel) mat.color.lerp(HL, .62 * kh);
      let op = 1;
      if (SHELL.has(type) && !inCarrier) op *= 1 - kx;
      if (!inCarrier) op *= 1 - .92 * kg;
      else if (!isSel) op *= 1 - .35 * kg;
      mat.transparent = op < .999;
      mat.opacity = op;
      mat.depthWrite = op > .5;
      mesh.visible = op > .02;
    }
    m.renderer.render(m.scene, m.camera);
    const p = cfCenter.clone().project(m.camera);
    meta.push({f, x: (p.x + 1) / 2, y: (1 - p.y) / 2, kh, kg});
    const blob = await new Promise((res) => canvas.toBlob(res, 'image/png'));
    await post(`/frame?i=${f}`, blob, 'image/png');
  }
  await post('/meta', JSON.stringify({frames: meta, width: canvas.width, height: canvas.height}), 'application/json');
  await post('/done', '{}', 'application/json');
}

main().catch(async (e) => {
  await post('/error', String(e && e.stack || e), 'text/plain');
});
