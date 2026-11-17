// langgraph-steps web interface. Plain JavaScript, no build step, no libraries.
// The server (lgsteps/server.py) runs the graph and sends one JSON line per
// thing LangGraph's stream reports; this file only draws what arrives.
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null) e.append(kid);
  return e;
}
function svg(tag, attrs, ...kids) {
  const e = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'text') e.textContent = v; else e.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null) e.append(kid);
  return e;
}

// ---- talking to the server ----------------------------------------------------
function request(url, body) {
  return fetch(url, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
}
async function json(url, body) {
  const res = await request(url, body);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
// Reads newline-delimited JSON as it arrives: one event per line.
async function* stream(url, body) {
  const res = await request(url, body);
  if (!res.ok) throw new Error((await res.json()).error || res.statusText);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, cut).trim();
      buffer = buffer.slice(cut + 1);
      if (line) yield JSON.parse(line);
    }
  }
  if (buffer.trim()) yield JSON.parse(buffer);
}

// ---- playback: the work takes milliseconds, the display is slowed --------------
const DELAYS = { instant: 0, normal: 650, slow: 1800 };
function pause() {
  const ms = DELAYS[$('#speed').value];
  return ms ? new Promise((r) => setTimeout(r, ms)) : null;
}

// ---- the graph drawing ---------------------------------------------------------
// Where each node sits is chosen by hand; which nodes and edges exist comes from the server.
const PLACES = {
  __start__: [60, 50, 80], retrieve: [190, 50, 110], grade: [400, 50, 110],
  rewrite: [190, 160, 110], approve: [400, 160, 110], generate: [590, 160, 110],
  give_up: [400, 270, 110], __end__: [590, 270, 80],
};
const NAMES = { __start__: 'START', __end__: 'END' };
// path, then where its label goes: x, y, text-anchor
const ROUTES = {
  '__start__>retrieve': ['M100,50 L133,50'],
  'retrieve>grade': ['M245,50 L343,50'],
  'grade>rewrite': ['M352,68 L247,141', 286, 116, 'end'],
  'rewrite>retrieve': ['M190,142 L190,70'],
  'grade>approve': ['M400,68 L400,140', 407, 108, 'start'],
  'grade>generate': ['M455,62 L549,141', 512, 96, 'start'],
  'grade>give_up': ['M372,68 C290,110 290,210 371,251', 318, 222, 'end'],
  'approve>generate': ['M455,160 L533,160', 494, 153, 'middle'],
  'approve>give_up': ['M400,178 L400,250', 407, 218, 'start'],
  'generate>__end__': ['M590,178 L590,250'],
  'give_up>__end__': ['M455,270 L548,270'],
};
const ABOUT = {
  retrieve: 'Searches the vector store for the query and counts the attempt.',
  grade: 'Keeps the paragraphs whose score reaches the threshold. After it, the conditional edge reads the state and picks the way.',
  rewrite: 'Changes the query: keeps only words the documents contain; on later tries it also drops the most common one.',
  approve: 'Calls interrupt(): the graph saves a checkpoint, stops and waits for a human.',
  generate: 'Writes the answer from the relevant paragraphs.',
  give_up: 'Ends the run without an answer.',
};
let graph = { nodes: [], edges: [] };

function drawGraph() {
  const root = $('#graph');
  root.replaceChildren(svg('defs', null,
    svg('marker', { id: 'arrow', viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' },
      svg('path', { d: 'M0,0 L10,5 L0,10 z' }))));
  for (const e of graph.edges) {
    const [x1, y1] = PLACES[e.source] || [0, 0], [x2, y2] = PLACES[e.target] || [0, 0];
    const [d, lx, ly, anchor] = ROUTES[`${e.source}>${e.target}`] || [`M${x1},${y1} L${x2},${y2}`, (x1 + x2) / 2, (y1 + y2) / 2, 'middle'];
    root.append(svg('g', { class: 'edge' + (e.conditional ? ' conditional' : ''), 'data-edge': `${e.source}>${e.target}` },
      svg('path', { d, 'marker-end': 'url(#arrow)' }),
      e.label ? svg('text', { x: lx, y: ly, 'text-anchor': anchor, text: e.label }) : null));
  }
  for (const name of graph.nodes) {
    const [x, y, w] = PLACES[name] || [40, 300, 110];
    const ends = name in NAMES;
    root.append(svg('g', { class: 'node' + (ends ? ' end-point' : ''), 'data-node': name },
      svg('rect', { x: x - w / 2, y: y - 18, width: w, height: 36, rx: ends ? 18 : 6 }),
      svg('text', { x, y, text: NAMES[name] || name }),
      svg('text', { class: 'visits', x: x + w / 2 - 5, y: y - 9, text: '' })));
  }
}
const nodeEl = (name) => $(`#graph [data-node="${name}"]`);
function takeEdge(from, to) {
  for (const e of document.querySelectorAll('#graph .edge.last')) e.classList.remove('last');
  const el = $(`#graph [data-edge="${from}>${to}"]`);
  if (el) el.classList.add('taken', 'last');
  return graph.edges.find((e) => e.source === from && e.target === to);
}

// ---- the state panel -----------------------------------------------------------
const KEYS = [
  ['question', 'never changes'], ['query', 'rewrite changes it'], ['attempts', 'searches so far'],
  ['documents', 'the last search'], ['relevant', 'passed the grade'], ['approved', "the human's answer"],
  ['answer', ''], ['log', 'reducer: appends'],
];
let threshold = 0.3;

function valueNode(key, value, before) {
  if (value === undefined) return 'not set';
  if (key === 'documents' || key === 'relevant') {
    if (!value.length) return '[ ]  (empty)';
    return h('ul', { class: 'val-list' }, value.map((d) => h('li', { class: d.score < threshold ? 'low' : '' },
      h('b', { text: d.score.toFixed(2) }), ` ${d.source} · ${d.heading}`)));
  }
  if (key === 'log') {
    const old = Array.isArray(before) ? before.length : 0;
    return h('ul', { class: 'val-list' }, value.map((line, i) => h('li', { class: before !== undefined && i >= old ? 'new' : '', text: line })));
  }
  return typeof value === 'string' ? `"${value}"` : String(value);
}
function stateRows(values, before, keys) {
  return keys.map(([key, note]) => h('tr', {
    class: (values[key] === undefined ? 'unset' : '') +
      (before && JSON.stringify(values[key]) !== JSON.stringify(before[key]) ? ' changed' : ''),
  }, h('th', null, key, note ? h('small', { text: note }) : null), h('td', null, valueNode(key, values[key], before ? before[key] : undefined))));
}
let shown = {};
function showState(values, note) {
  $('#state').replaceChildren(...stateRows(values, shown, KEYS));
  shown = values;
  if (note) $('#state-note').textContent = note;
}

// ---- a run ---------------------------------------------------------------------
let busy = false;
let run = null;         // { thread, prev, card, steps, visits }

function setBusy(on) {
  busy = on;
  for (const id of ['#run', '#approve-yes', '#approve-no']) $(id).disabled = on;
}
function progress(fraction, text, isError) {
  $('#run-bar').style.width = Math.round(Math.min(1, fraction) * 100) + '%';
  const label = $('#run-label');
  label.textContent = text;
  label.classList.toggle('error', !!isError);
}
function settings() {
  return { threshold: +$('#threshold').value, max_attempts: +$('#attempts').value, ask_approval: $('#approval').checked };
}
function fractionDone() {
  return Math.min(0.92, run.steps / (3 * (+$('#attempts').value) + 2));
}

function newCard(node) {
  const card = h('article', { class: 'step run' },
    h('header', null, h('span', { class: 'num', text: run.steps }), h('h3', { text: node }), h('span', { class: 'status', text: 'running' })),
    h('p', { class: 'what', text: ABOUT[node] || '' }));
  $('#timeline').append(card);
  card.scrollIntoView({ block: 'nearest' });
  return card;
}
function cardStatus(state, text) {
  run.card.classList.remove('run', 'done', 'skip');
  if (state) run.card.classList.add(state);
  $('.status', run.card).textContent = text;
}
function cardThen(text, target) {
  $('.then', run.card)?.remove();
  run.card.append(h('div', { class: 'then' }, text, h('b', { text: target })));
}

async function onEvent(ev) {
  switch (ev.step) {
    case 'start':
      run.thread = ev.thread_id;
      $('#f-thread').textContent = ev.thread_id;
      break;

    case 'node_start': {
      const resumed = run.waiting === ev.node;        // a resumed node starts again from its first line
      run.waiting = null;
      nodeEl(ev.node).classList.remove('waiting');
      nodeEl(ev.node).classList.add('active');
      if (resumed) { cardStatus('run', 'resumed'); break; }
      const edge = takeEdge(run.prev, ev.node);
      if (run.card) cardThen(edge && edge.label ? `conditional edge chose "${edge.label}" → ` : 'edge → ', ev.node);
      run.steps += 1;
      run.visits[ev.node] = (run.visits[ev.node] || 0) + 1;
      $('.visits', nodeEl(ev.node)).textContent = run.visits[ev.node] > 1 ? '×' + run.visits[ev.node] : '';
      run.card = newCard(ev.node);
      progress(fractionDone(), `Step ${run.steps}: ${ev.node} is running`);
      await pause();
      break;
    }

    case 'node_end': {
      const el = nodeEl(ev.node);
      el.classList.remove('active');
      if (ev.interrupted) {
        el.classList.add('waiting');
        run.waiting = ev.node;
        cardStatus('run', 'paused by interrupt()');
        break;
      }
      el.classList.add('visited');
      run.prev = ev.node;
      const keys = Object.keys(ev.update).map((k) => [k, '']);
      run.card.append(h('div', { class: 'update' },
        h('div', { text: `returned an update to ${keys.length} key${keys.length === 1 ? '' : 's'}` }),
        h('table', { class: 'state' }, h('tbody', null, stateRows(ev.update, null, keys)))));
      cardStatus('done', `${ev.ms} ms`);
      break;
    }

    case 'state':
      showState(ev.values, run.prev === '__start__' ? 'the input, before any node ran' : `after ${run.prev}: the marked keys changed`);
      if (run.prev !== '__start__') await pause();
      break;

    case 'interrupt':
      $('#approval-text').textContent = `It wants to answer "${ev.value.question}" from: ${ev.value.sources.join('; ')}.`;
      break;

    case 'paused':
      $('#approval-box').classList.add('on');
      showCheckpoints(ev.checkpoints);
      progress(fractionDone(), `Paused before ${ev.next.join(', ')}: waiting for your answer. The state is saved in a checkpoint.`);
      break;

    case 'done':
      takeEdge(run.prev, '__end__');
      nodeEl('__end__').classList.add('visited');
      if (run.card) cardThen('edge → ', 'END');
      $('#answer').textContent = ev.values.answer ?? '';
      $('#answer-note').textContent = `state["answer"], written by ${run.prev}`;
      showCheckpoints(ev.checkpoints);
      progress(1, `Done: ${run.steps} steps, ${ev.values.attempts} search${ev.values.attempts === 1 ? '' : 'es'}, ended in ${run.prev}.`);
      break;

    case 'error':
      throw new Error(ev.message);
  }
}

function showCheckpoints(list) {
  const after = (i) => (i === 0 ? 'the input' : list[i - 1].next.join(', ') || '–');
  $('#checkpoints').replaceChildren(h('table', { class: 'rows' },
    h('thead', null, h('tr', null, ['step', 'saved after', 'next', 'query', 'tries', 'log'].map((t) => h('th', { text: t })))),
    h('tbody', null, list.map((c, i) => h('tr', { title: `checkpoint ${c.id}\n${c.time}` },
      h('td', { text: c.step }), h('td', { text: after(i) }), h('td', { text: c.next.join(', ') || 'END' }),
      h('td', { text: c.query == null ? '–' : `"${c.query.length > 26 ? c.query.slice(0, 26) + '…' : c.query}"` }),
      h('td', { text: c.attempts }), h('td', { text: c.log }))))));
  $('#cp-note').textContent = `${list.length} checkpoints in thread ${run.thread}`;
}

async function play(url, body) {
  setBusy(true);
  try {
    for await (const ev of stream(url, body)) await onEvent(ev);
  } catch (err) {
    if (run.card && run.card.classList.contains('run')) cardStatus('', 'failed');
    progress(0, 'Failed: ' + err.message, true);
  } finally {
    setBusy(false);
  }
}

async function startRun() {
  if (busy) return;
  const question = $('#question').value.trim();
  if (!question) { progress(0, 'Type a question first.', true); return; }
  run = { thread: null, prev: '__start__', card: null, steps: 0, visits: {}, waiting: null };
  threshold = +$('#threshold').value;
  drawGraph();
  nodeEl('__start__').classList.add('visited');
  shown = {};
  $('#timeline').replaceChildren();
  $('#approval-box').classList.remove('on');
  $('#answer').replaceChildren(h('span', { class: 'empty', text: 'Not there yet.' }));
  $('#checkpoints').replaceChildren(h('p', { class: 'empty', text: 'Listed when the run stops.' }));
  progress(0, 'Starting…');
  await play('/api/run', { question, ...settings() });
}

async function resume(approved) {
  if (busy || !run || !run.waiting) return;
  $('#approval-box').classList.remove('on');
  progress(fractionDone(), `Resuming thread ${run.thread} with Command(resume=${approved ? 'True' : 'False'})…`);
  await play('/api/resume', { thread_id: run.thread, approved });
}

// ---- wiring --------------------------------------------------------------------
async function init() {
  const state = await json('/api/state');
  $('#f-docs').textContent = state.documents;
  $('#f-paragraphs').textContent = state.paragraphs;
  $('#f-saver').textContent = state.checkpointer;
  graph = state.graph;
  drawGraph();
  showState({});
  shown = {};

  $('#question').value = $('#samples option').value;
  $('#threshold').value = state.defaults.threshold;
  $('#attempts').value = state.defaults.max_attempts;
  $('#approval').checked = state.defaults.ask_approval;
  const showThreshold = () => { $('#threshold-out').textContent = (+$('#threshold').value).toFixed(2); };
  showThreshold();
  $('#threshold').addEventListener('input', showThreshold);
  $('#run-form').addEventListener('submit', (e) => { e.preventDefault(); startRun(); });
  $('#approve-yes').addEventListener('click', () => resume(true));
  $('#approve-no').addEventListener('click', () => resume(false));

  // http://127.0.0.1:8767/?autorun  runs once without playback delay (add &approval to pause for a human).
  const params = new URLSearchParams(location.search);
  if (params.has('autorun')) {
    $('#speed').value = 'instant';
    if (params.get('autorun')) $('#question').value = params.get('autorun');
    if (params.has('approval')) $('#approval').checked = true;
    await startRun();
    window.scrollTo(0, 0);
    document.title = 'ready · ' + document.title;
  }
}

init().catch((err) => progress(0, 'Could not reach the server: ' + err.message, true));
