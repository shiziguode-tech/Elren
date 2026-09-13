// Real pinned vendor lifecycle methods with a small synthetic DOM boundary.
// No browser, service, network, or user page is created by this fixture.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
for (const name of ['node:net', 'node:http', 'node:https']) {
  const module = require(name);
  for (const method of ['connect', 'createConnection', 'request', 'get']) {
    if (module[method]) module[method] = () => { throw Error('Network forbidden'); };
  }
}
const [root, scenario, transition, language] = process.argv.slice(2);
const errors = [];
class Element {
  constructor() { this.listeners = new Map(); this.children = []; this.style = {}; this.attributes = {}; this.value = ''; this.nodeName = 'INPUT'; }
  addEventListener(name, callback, options) {
    const list = this.listeners.get(name) || [];
    list.push({callback, once: Boolean(options?.once)}); this.listeners.set(name, list);
  }
  removeEventListener(name, callback) { this.listeners.set(name, (this.listeners.get(name) || []).filter(item => item.callback !== callback)); }
  emit(name) {
    for (const item of [...(this.listeners.get(name) || [])]) {
      if (item.once) this.removeEventListener(name, item.callback);
      try { item.callback({type:name}); } catch (error) { errors.push(String(error)); }
    }
  }
  dispatchEvent(event) { this.emit(event.type); }
  appendChild(child) { this.children.push(child); child.parentNode = this; }
  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index < 0) throw Error('removeChild: node is not a child');
    this.children.splice(index, 1); child.parentNode = null;
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  getBoundingClientRect() { return {left:20,top:50,bottom:90,width:200,height:40}; }
  blur() {}
}
const dialog = new Element();
const inputs = [new Element(), new Element()];
global.document = {querySelectorAll: () => inputs};
global.window = {innerWidth:1280, innerHeight:720, ElrenDateLocales:{en:{},zh:{}},
  getComputedStyle: () => ({getPropertyValue: () => transition === 'animated' ? '0.2s' : '0s'})};
global.$ = () => dialog;
global.isEnglish = () => language === 'en';
global.uiText = (zh,en) => isEnglish() ? en : zh;
const Vendor = require(path.join(root, 'deepdesk/static/vendor/air-datepicker/air-datepicker.js'));
const pickers = [];
window.AirDatepicker = function(input, opts) {
  // A null target lets the real constructor install its bound lifecycle
  // methods without constructing an entire calendar grid in this DOM fixture.
  const picker = new Vendor(null);
  Object.assign(picker, {opts, $el:input, $datepicker:new Element(), $customContainer:dialog,
    elIsInput:true, visible:false, hideAnimation:false, customHide:false, views:{},
    currentView:'days', created:0, destroyed:0, hideCalls:0});
  picker.$datepicker.classList = {add(){},remove(){}};
  picker.$datepicker.offsetWidth = 280; picker.$datepicker.offsetHeight = 360;
  picker._createComponents = function() {
    this.created++;
    dialog.appendChild(this.$datepicker);
    this.views = {days:{destroy(){}}};
    this.nav = {destroy: () => this.destroyed++};
    this.timepicker = {destroy(){}};
    // These names are checked against the unchanged vendor HTML in pytest.
    this.ranges = {hours:new Element(),minutes:new Element()};
    this.$datepicker.querySelector = selector => this.ranges[selector.match(/name="(hours|minutes)"/)[1]];
  };
  const originalHide = picker.hide;
  picker.hide = function() { this.hideCalls++; return originalHide.call(this); };
  pickers.push(picker);
  return picker;
};
vm.runInThisContext(fs.readFileSync(0, 'utf8'));
initializeScheduleDatePickers();
const picker = pickers[0];
const finish = () => picker.$datepicker.emit('transitionend');
if (scenario === 'never') {
  dialog.emit('close'); dialog.emit('close'); dialog.emit('scroll');
} else if (scenario === 'hidden') {
  picker.show(); picker.hide(); finish();
  dialog.emit('close'); dialog.emit('scroll'); dialog.emit('close');
} else if (scenario === 'closing') {
  picker.show(); picker.hide();
  dialog.emit('close'); dialog.emit('scroll'); finish(); dialog.emit('close');
} else if (scenario === 'visible') {
  picker.show(); dialog.emit('close'); dialog.emit('scroll'); finish();
} else if (scenario === 'reopen-during-close') {
  picker.show(); picker.hide(); picker.show();
  // hideAnimation can still be true after show cancelled the old transition.
  dialog.emit('close'); finish(); dialog.emit('close');
} else if (scenario === 'repeat-reopen') {
  for (let index=0; index<4; index++) {
    picker.show(); dialog.emit('scroll'); dialog.emit('close'); finish(); dialog.emit('close');
  }
} else if (scenario === 'labels') {
  picker.show();
  const first = Object.fromEntries(Object.entries(picker.ranges).map(([key,value]) => [key,value.attributes['aria-label']]));
  dialog.emit('close'); finish(); picker.show();
  const second = Object.fromEntries(Object.entries(picker.ranges).map(([key,value]) => [key,value.attributes['aria-label']]));
  console.log(JSON.stringify({first,second,errors})); process.exit(0);
} else throw Error('Unknown fixture scenario');
console.log(JSON.stringify({errors,hideCalls:pickers.map(item=>item.hideCalls),created:picker.created,
  destroyed:picker.destroyed,visible:picker.visible,children:dialog.children.length}));
