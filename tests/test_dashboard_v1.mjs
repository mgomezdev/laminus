import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const html = readFileSync(new URL('../app/templates/index.html', import.meta.url), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];

test('dashboard paginates profiles, submits UUIDs, handles typed events and artifact availability', async () => {
    const elements = new Map();
    const element = () => ({style: {}, children: [], listeners: {}, value: '', checked: false,
        innerHTML: '', textContent: '', classList: {add() {}, remove() {}},
        appendChild(child) { this.children.push(child); }, querySelector() { return null; },
        addEventListener(name, fn) { this.listeners[name] = fn; }});
    const get = id => {
        if (!elements.has(id)) elements.set(id, element());
        return elements.get(id);
    };
    const requests = [], streams = [];
    const job = {id: 'job-1', status: 'completed', artifact: {name: 'part.gcode', available: true}};
    const context = vm.createContext({
        console, URLSearchParams, FormData, Date, Math, setInterval() {}, clearInterval() {},
        setTimeout() {}, document: {getElementById: get, createElement: element, head: element()},
        window: {location: {}},
        EventSource: class {
            constructor(url) { this.url = url; this.listeners = {}; streams.push(this); }
            addEventListener(name, fn) { this.listeners[name] = fn; }
            close() { this.closed = true; }
        },
        fetch: async (url, options = {}) => {
            requests.push({url, options});
            assert.ok(url.startsWith('/api/v1/'), `versionless request: ${url}`);
            let body;
            if (url.startsWith('/api/v1/profiles')) {
                body = url.includes('cursor=')
                    ? {data: [{type: 'filament', uuid: 'f1', name: 'PLA'}], page: {next_cursor: null}}
                    : {data: [{type: 'machine', uuid: 'm1', name: 'Printer'}, {type: 'process', uuid: 'p1', name: 'Process'}], page: {next_cursor: 'page-2'}};
            } else if (options.method === 'POST') body = job;
            else if (url === '/api/v1/jobs?limit=50') body = {data: [job], page: {next_cursor: null}};
            else body = job;
            return {ok: true, json: async () => body};
        },
    });
    vm.runInContext(script, context);
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(requests.some(r => r.url.includes('cursor=page-2')));
    assert.equal(get('machine-select').children[0].value, 'm1');
    assert.equal(get('filament-select').children[0].value, 'f1');
    get('machine-select').value = 'm1'; get('process-select').value = 'p1';
    get('filament-select').value = 'f1'; get('plate-select').value = '1';
    vm.runInContext("handleFileSelect({name: 'part.stl', size: 5})", context);
    await get('slice-form').listeners.submit({preventDefault() {}});
    const create = requests.find(r => r.url === '/api/v1/jobs' && r.options.method === 'POST');
    assert.equal(create.url, '/api/v1/jobs');
    assert.equal(create.options.body.get('machine_uuid'), 'm1');
    assert.equal(create.options.body.get('process_uuid'), 'p1');
    assert.equal(create.options.body.get('filament_uuids'), '["f1"]');
    assert.equal(streams[0].url, '/api/v1/jobs/job-1/events');
    streams[0].listeners.log({data: JSON.stringify({message: 'Slicing part'})});
    streams[0].listeners.completed({data: '{}'});
    assert.equal(streams[0].closed, true);
    get('download-btn').onclick();
    assert.equal(context.window.location.href, '/api/v1/jobs/job-1/artifact');
    await vm.runInContext('fetchJobs()', context);
    assert.match(get('jobs-tbody').innerHTML, /\/api\/v1\/jobs\/job-1\/artifact/);
    job.artifact.available = false;
    await vm.runInContext('fetchJobs()', context);
    assert.doesNotMatch(get('jobs-tbody').innerHTML, /jobs-download/);
});
