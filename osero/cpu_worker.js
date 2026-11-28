// UltraOSERO: CPU の思考を別スレッドで行う（server.js の worker_threads プールから使う）。
// メインのイベントループを止めないため、重い探索はここで行う。
'use strict';
const { parentPort } = require('worker_threads');
const E = require('./engine.js');

parentPort.on('message', (q) => {
  let move = -1;
  try { move = E.think(E.fromData(q.data), q.level, { time: q.time }); } catch (e) { move = -1; }
  parentPort.postMessage({ id: q.id, move });
});
