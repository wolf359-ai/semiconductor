import { NodeIO } from '@gltf-transform/core';
import { ALL_EXTENSIONS } from '@gltf-transform/extensions';
import { prune, dedup, textureCompress, meshopt, weld } from '@gltf-transform/functions';
import { MeshoptEncoder } from 'meshoptimizer';
import sharp from 'sharp';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const M = path.resolve(HERE, '..');
const OUT = path.join(HERE, 'out');

await MeshoptEncoder.ready;
fs.mkdirSync(OUT, { recursive: true });
const io = new NodeIO().registerExtensions(ALL_EXTENSIONS).registerDependencies({ 'meshopt.encoder': MeshoptEncoder });

const ORANGE = /^(base|base_parts_01|base_accent_01|hydraulic0[12]_pumps|wheel_metal\.[BF])$/, SEAT = /^chair(_sides)?$/;

async function recolour(doc) {
  const done = new Set();
  for (const m of doc.getRoot().listMaterials()) {
    const t = m.getBaseColorTexture(), n = m.getName();
    if (!t || done.has(t)) continue;
    let img = null;
    if (ORANGE.test(n)) img = sharp(Buffer.from(t.getImage())).modulate({ hue: 16, saturation: 1.18, brightness: 1.06 });
    else if (SEAT.test(n)) img = sharp(Buffer.from(t.getImage())).modulate({ saturation: 0.12, brightness: 0.62 });
    if (!img) continue;
    t.setImage(new Uint8Array(await img.png().toBuffer())).setMimeType('image/png');
    done.add(t);
  }
  console.log('recoloured', done.size, 'textures');
}

async function pack(src, dst, { keepAnims, size, tint }) {
  const doc = await io.read(path.join(M, src));
  const root = doc.getRoot();
  if (tint) await recolour(doc);
  if (!keepAnims) root.listAnimations().forEach(a => a.dispose());
  root.listCameras().forEach(c => c.dispose());
  root.listNodes().filter(n => /camera|preset_light/i.test(n.getName())).forEach(n => n.dispose());
  await doc.transform(
    dedup(), weld(),
    textureCompress({ encoder: sharp, targetFormat: 'webp', resize: [size, size], quality: 82 }),
    prune({ keepLeaves: true }),
    meshopt({ encoder: MeshoptEncoder, level: 'medium' })
  );
  const out = path.join(OUT, dst);
  await io.write(out, doc);
  console.log(dst, (fs.statSync(out).size / 1e6).toFixed(2), 'MB, nodes', root.listNodes().length, 'anims', root.listAnimations().length);
}
await pack('forklift.glb', 'forklift.glb', { keepAnims: false, size: 1024, tint: true });
await pack('industrial_-_3d_agv__trolley_-_omrom.glb', 'agv.glb', { keepAnims: false, size: 512 });
