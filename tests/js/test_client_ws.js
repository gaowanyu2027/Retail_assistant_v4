// 本地摄像头前端链路的回归测试（台账 D13）：openSource({kind:'client'}) 必须开出上行通道
//
// 为什么要有这个测试：这个 bug 是"纯前端漏了一步"，服务端任何测试都发现不了 ——
// 端点级测试自己连了 /api/ws/client 发帧，于是服务端看起来完全正常，
// 而真实前端每帧都被静默丢掉、界面永久停在"正在打开 client…"。
// 这里用打桩的 WebSocket + location，把 stream.js 当经典脚本加载后直接验行为。

const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', '..', 'frontend-vue', 'public', 'js', 'stream.js');

let failures = [];
function check(name, cond, extra) {
  if (cond) { console.log(`  PASS  ${name}`); }
  else { console.log(`  FAIL  ${name}${extra ? ' -> ' + extra : ''}`); failures.push(name); }
}

// ---- 打桩运行环境 ----
const created = [];
class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 1;                 // OPEN
    this.sent = [];
    created.push(this);
  }
  send(d) { this.sent.push(d); }
  close() { this.readyState = 3; }
}
FakeWebSocket.OPEN = 1;
FakeWebSocket.CONNECTING = 0;
FakeWebSocket.CLOSING = 2;
FakeWebSocket.CLOSED = 3;

const statuses = [];
global.WebSocket = FakeWebSocket;
global.location = { protocol: 'http:', host: '127.0.0.1:8000' };
global.window = { updateStatus: (s, m) => statuses.push([s, m]) };
global.setTimeout = setTimeout;
global.clearTimeout = clearTimeout;

// stream.js 是经典脚本（顶层 const StreamManager），用 new Function 包一层把它的
// 绑定"取出来"（直接 eval 的话 const 会留在 eval 自己的作用域里，外面取不到）
const code = fs.readFileSync(SRC, 'utf8');
const StreamManager = new Function(code + '\nreturn StreamManager;')();

console.log('=== 1 建流通道的行为：openSource({kind:"client"}) 必须开 /api/ws/client ===');
created.length = 0;
StreamManager.openSource({ kind: 'client' });
const clientWs = created.filter(w => w.url.includes('/api/ws/client'));
check('openSource({kind:client}) 会创建 /api/ws/client', clientWs.length === 1,
      `创建的 URL: ${created.map(w => w.url).join(', ')}`);

console.log('\n=== 2 非 client 的源不应多开这条通道（避免无谓连接）===');
created.length = 0;
StreamManager.openSource({ kind: 'camera', id: 'cam_in_01' });
check('openSource({kind:camera}) 不创建 /api/ws/client',
      created.filter(w => w.url.includes('/api/ws/client')).length === 0,
      `创建的 URL: ${created.map(w => w.url).join(', ')}`);

console.log('\n=== 3 帧真的发出去了（而不是被静默丢弃）===');
created.length = 0;
StreamManager.openSource({ kind: 'client' });
const ws = created.find(w => w.url.includes('/api/ws/client'));
const payload = new Uint8Array([1, 2, 3, 4]);
const ok = StreamManager.sendClientFrame(payload);
check('sendClientFrame 返回 true 且帧已入队', ok === true && ws.sent.length === 1,
      `ok=${ok} sent=${ws.sent.length}`);
check('发出去的就是原始帧字节', ws.sent[0] === payload);

console.log('\n=== 4 通道没建时不能静默：要计数并给一次提示 ===');
StreamManager.closeClientStream();
StreamManager.clientDroppedFrames = 0;
statuses.length = 0;
for (let i = 0; i < 10; i++) StreamManager.sendClientFrame(payload);
check('丢帧被计数（不再静默）', StreamManager.clientDroppedFrames === 10,
      `clientDroppedFrames=${StreamManager.clientDroppedFrames}`);
check('第 10 帧触发一次可见提示', statuses.length === 1 && statuses[0][0] === 'error',
      JSON.stringify(statuses));

console.log('\n=== 5 stop() 要关掉上行通道（否则下次打开会串）===');
StreamManager.openSource({ kind: 'client' });
StreamManager.stop();
check('stop() 后 clientWs 已清空', StreamManager.clientWs === null,
      `clientWs=${StreamManager.clientWs}`);

console.log(`\n${failures.length ? '存在失败: ' + failures.join(', ') : '全部通过'}`);
process.exit(failures.length ? 1 : 0);
