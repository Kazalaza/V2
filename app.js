const state = {
  gameDirHandle: null,
  modsDirHandle: null,
  mods: [],
  presets: loadJSON('v2Launcher.presets', {}),
  loadOrder: loadJSON('v2Launcher.loadOrder', []),
  session: loadJSON('v2Launcher.session', {}),
};

const els = {
  pickGameDirBtn: byId('pickGameDirBtn'),
  gameDirLabel: byId('gameDirLabel'),
  gameVersion: byId('gameVersion'),
  gameChecksum: byId('gameChecksum'),
  scanModsBtn: byId('scanModsBtn'),
  modList: byId('modList'),
  modTemplate: byId('modTemplate'),
  emptyState: byId('emptyState'),
  modsSummary: byId('modsSummary'),
  modSearch: byId('modSearch'),
  filterState: byId('filterState'),
  presetNameInput: byId('presetNameInput'),
  savePresetBtn: byId('savePresetBtn'),
  presetSelect: byId('presetSelect'),
  loadPresetBtn: byId('loadPresetBtn'),
  deletePresetBtn: byId('deletePresetBtn'),
  autoloadLastPreset: byId('autoloadLastPreset'),
  windowed: byId('windowed'),
  borderless: byId('borderless'),
  resWidth: byId('resWidth'),
  resHeight: byId('resHeight'),
  extraArgs: byId('extraArgs'),
  launchBtn: byId('launchBtn'),
};

initialize();

function initialize() {
  restoreSessionSettings();
  wireEvents();
  renderPresetOptions();

  if (state.session.lastMods) {
    // staged restore happens after scanning.
    state.loadOrder = state.session.lastMods;
  }

  if (state.session.autoLoadLastPreset && state.session.lastPresetName) {
    const preset = state.presets[state.session.lastPresetName];
    if (preset) applyPreset(state.session.lastPresetName);
  }

  renderMods();
}

function wireEvents() {
  els.pickGameDirBtn.addEventListener('click', pickGameDirectory);
  els.scanModsBtn.addEventListener('click', scanMods);
  els.modSearch.addEventListener('input', renderMods);
  els.filterState.addEventListener('change', renderMods);

  els.savePresetBtn.addEventListener('click', savePresetFromUI);
  els.loadPresetBtn.addEventListener('click', () => applyPreset(els.presetSelect.value));
  els.deletePresetBtn.addEventListener('click', deleteSelectedPreset);
  els.autoloadLastPreset.addEventListener('change', persistSession);

  [els.windowed, els.borderless, els.resWidth, els.resHeight, els.extraArgs].forEach((el) => {
    el.addEventListener('change', persistSession);
    el.addEventListener('input', persistSession);
  });

  els.launchBtn.addEventListener('click', launchGame);
}

async function pickGameDirectory() {
  if (!window.showDirectoryPicker) {
    alert('Directory access requires File System Access API (Chromium/Tauri/Electron host).');
    return;
  }

  try {
    const dir = await window.showDirectoryPicker({ mode: 'read' });
    state.gameDirHandle = dir;
    els.gameDirLabel.textContent = dir.name;

    const modsHandle = await getDirectoryHandleSafe(dir, 'mod');
    state.modsDirHandle = modsHandle;

    const versionText = await readTextFileSafe(dir, 'version.txt');
    if (versionText) {
      const [version, checksum] = parseVersionChecksum(versionText);
      els.gameVersion.textContent = version || 'Unknown';
      els.gameChecksum.textContent = checksum || 'N/A';
    }

    persistSession();
  } catch (error) {
    console.error(error);
    alert('Could not access selected directory.');
  }
}

async function scanMods() {
  if (!state.modsDirHandle) {
    alert('Select Victoria II directory first (with /mod folder).');
    return;
  }

  const modFiles = [];
  for await (const [name, handle] of state.modsDirHandle.entries()) {
    if (handle.kind === 'file' && name.endsWith('.mod')) {
      modFiles.push(handle);
    }
  }

  const parsedMods = [];
  for (const fileHandle of modFiles) {
    const raw = await readFileHandleText(fileHandle);
    const descriptor = parseModDescriptor(raw);
    const key = descriptor.path || stripExt(fileHandle.name);
    const modDir = descriptor.path ? await getDirectoryHandleSafe(state.modsDirHandle, descriptor.path) : null;
    const icon = modDir ? await findIconBlobUrl(modDir) : null;

    parsedMods.push({
      id: fileHandle.name,
      key,
      name: descriptor.name || prettifyName(stripExt(fileHandle.name)),
      path: descriptor.path || '',
      description: descriptor.description || 'No description available.',
      dependencies: descriptor.dependencies,
      replacePaths: descriptor.replacePath,
      icon,
      enabled: state.loadOrder.includes(fileHandle.name),
      conflict: false,
      conflictReasons: [],
    });
  }

  state.mods = parsedMods;
  detectConflicts();

  if (state.loadOrder.length) {
    const map = new Map(state.mods.map((m) => [m.id, m]));
    state.mods = state.loadOrder.map((id) => map.get(id)).filter(Boolean).concat(state.mods.filter((m) => !state.loadOrder.includes(m.id)));
  }

  renderMods();
  persistSession();
}

function detectConflicts() {
  const replaceMap = new Map();

  for (const mod of state.mods) {
    for (const path of mod.replacePaths) {
      if (!replaceMap.has(path)) replaceMap.set(path, []);
      replaceMap.get(path).push(mod.id);
    }
  }

  for (const mod of state.mods) {
    mod.conflict = false;
    mod.conflictReasons = [];

    for (const dep of mod.dependencies) {
      const depFound = state.mods.some((m) => m.name === dep || stripExt(m.id) === dep || m.path === dep);
      if (!depFound) {
        mod.conflict = true;
        mod.conflictReasons.push(`Missing dependency: ${dep}`);
      }
    }

    for (const path of mod.replacePaths) {
      const owners = replaceMap.get(path) || [];
      if (owners.length > 1) {
        mod.conflict = true;
        mod.conflictReasons.push(`Overwrites shared path: ${path}`);
      }
    }
  }
}

function renderMods() {
  const query = els.modSearch.value.trim().toLowerCase();
  const filter = els.filterState.value;
  els.modList.innerHTML = '';

  const filtered = state.mods.filter((mod) => {
    const matchesQuery = !query || [mod.name, mod.description, mod.path, mod.id].join(' ').toLowerCase().includes(query);
    if (!matchesQuery) return false;
    if (filter === 'enabled') return mod.enabled;
    if (filter === 'disabled') return !mod.enabled;
    if (filter === 'conflicts') return mod.conflict;
    return true;
  });

  filtered.forEach((mod, index) => {
    const item = els.modTemplate.content.firstElementChild.cloneNode(true);
    item.dataset.modId = mod.id;

    const iconEl = item.querySelector('.mod-icon');
    iconEl.src = mod.icon || defaultIcon();

    const toggle = item.querySelector('.mod-toggle');
    toggle.checked = mod.enabled;
    toggle.addEventListener('change', () => {
      mod.enabled = toggle.checked;
      persistSession();
      updateSummary();
    });

    item.querySelector('.mod-name').textContent = mod.name;
    item.querySelector('.mod-description').textContent = mod.description;
    item.querySelector('.mod-id').textContent = mod.id;
    item.querySelector('.mod-deps').textContent = mod.dependencies.length
      ? `Deps: ${mod.dependencies.join(', ')}`
      : 'No dependencies';

    const tag = item.querySelector('.conflict-tag');
    if (mod.conflict) {
      tag.classList.remove('hidden');
      item.title = mod.conflictReasons.join('\n');
    }

    setupDragAndDrop(item, index);
    els.modList.appendChild(item);
  });

  els.emptyState.classList.toggle('hidden', filtered.length > 0);
  updateSummary();
}

function setupDragAndDrop(item, index) {
  item.addEventListener('dragstart', () => {
    item.classList.add('dragging');
    item.dataset.dragIndex = index;
  });

  item.addEventListener('dragend', () => {
    item.classList.remove('dragging');
    delete item.dataset.dragIndex;
  });

  item.addEventListener('dragover', (event) => {
    event.preventDefault();
  });

  item.addEventListener('drop', (event) => {
    event.preventDefault();
    const dragItem = document.querySelector('.mod-item.dragging');
    if (!dragItem || dragItem === item) return;

    const fromId = dragItem.dataset.modId;
    const toId = item.dataset.modId;
    const fromIndex = state.mods.findIndex((m) => m.id === fromId);
    const toIndex = state.mods.findIndex((m) => m.id === toId);
    if (fromIndex < 0 || toIndex < 0) return;

    const [moved] = state.mods.splice(fromIndex, 1);
    state.mods.splice(toIndex, 0, moved);
    state.loadOrder = state.mods.map((m) => m.id);
    persistSession();
    renderMods();
  });
}

function updateSummary() {
  const enabled = state.mods.filter((mod) => mod.enabled).length;
  const conflicts = state.mods.filter((mod) => mod.conflict).length;
  els.modsSummary.textContent = `${state.mods.length} mods • ${enabled} enabled • ${conflicts} conflicts`;
}

function savePresetFromUI() {
  const name = els.presetNameInput.value.trim();
  if (!name) return;

  state.presets[name] = {
    mods: state.mods.filter((m) => m.enabled).map((m) => m.id),
    loadOrder: state.mods.map((m) => m.id),
    launch: collectLaunchOptions(),
    savedAt: new Date().toISOString(),
  };

  state.session.lastPresetName = name;
  saveJSON('v2Launcher.presets', state.presets);
  persistSession();
  renderPresetOptions();
  els.presetSelect.value = name;
}

function applyPreset(name) {
  const preset = state.presets[name];
  if (!preset) return;

  const enabledSet = new Set(preset.mods || []);
  for (const mod of state.mods) {
    mod.enabled = enabledSet.has(mod.id);
  }

  if (Array.isArray(preset.loadOrder) && preset.loadOrder.length) {
    state.loadOrder = preset.loadOrder;
    const sorted = preset.loadOrder
      .map((id) => state.mods.find((m) => m.id === id))
      .filter(Boolean);
    state.mods = sorted.concat(state.mods.filter((m) => !preset.loadOrder.includes(m.id)));
  }

  if (preset.launch) {
    els.windowed.checked = Boolean(preset.launch.windowed);
    els.borderless.checked = Boolean(preset.launch.borderless);
    els.resWidth.value = preset.launch.width || '';
    els.resHeight.value = preset.launch.height || '';
    els.extraArgs.value = preset.launch.extraArgs || '';
  }

  state.session.lastPresetName = name;
  persistSession();
  renderMods();
}

function deleteSelectedPreset() {
  const selected = els.presetSelect.value;
  if (!selected) return;
  delete state.presets[selected];
  if (state.session.lastPresetName === selected) {
    delete state.session.lastPresetName;
  }
  saveJSON('v2Launcher.presets', state.presets);
  persistSession();
  renderPresetOptions();
}

function renderPresetOptions() {
  const names = Object.keys(state.presets).sort((a, b) => a.localeCompare(b));
  els.presetSelect.innerHTML = names.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join('');
}

function collectLaunchOptions() {
  return {
    windowed: els.windowed.checked,
    borderless: els.borderless.checked,
    width: Number(els.resWidth.value) || null,
    height: Number(els.resHeight.value) || null,
    extraArgs: els.extraArgs.value.trim(),
  };
}

function launchGame() {
  const payload = {
    game: 'Victoria II',
    exe: 'v2game.exe',
    enabledMods: state.mods.filter((m) => m.enabled).map((m) => m.id),
    loadOrder: state.mods.map((m) => m.id),
    launchOptions: collectLaunchOptions(),
    version: els.gameVersion.textContent,
    checksum: els.gameChecksum.textContent,
    generatedAt: new Date().toISOString(),
  };

  persistSession();

  window.dispatchEvent(new CustomEvent('v2-launch', { detail: payload }));
  console.log('Launch payload', payload);
  alert('Launch payload emitted. Connect this event to your native app launcher bridge.');
}

function restoreSessionSettings() {
  els.autoloadLastPreset.checked = Boolean(state.session.autoLoadLastPreset);
  els.windowed.checked = Boolean(state.session.windowed);
  els.borderless.checked = Boolean(state.session.borderless);
  els.resWidth.value = state.session.width || '';
  els.resHeight.value = state.session.height || '';
  els.extraArgs.value = state.session.extraArgs || '';
}

function persistSession() {
  state.session.autoLoadLastPreset = els.autoloadLastPreset.checked;
  state.session.windowed = els.windowed.checked;
  state.session.borderless = els.borderless.checked;
  state.session.width = Number(els.resWidth.value) || null;
  state.session.height = Number(els.resHeight.value) || null;
  state.session.extraArgs = els.extraArgs.value.trim();
  state.session.lastMods = state.mods.map((m) => m.id);
  state.session.enabledMods = state.mods.filter((m) => m.enabled).map((m) => m.id);

  saveJSON('v2Launcher.loadOrder', state.mods.map((m) => m.id));
  saveJSON('v2Launcher.session', state.session);
}

function parseModDescriptor(text) {
  const getString = (key) => {
    const match = text.match(new RegExp(`${key}\\s*=\\s*"([^"]+)"`, 'i'));
    return match ? match[1].trim() : '';
  };

  const getList = (key) => {
    const block = text.match(new RegExp(`${key}\\s*=\\s*\\{([^}]+)\\}`, 'i'));
    if (!block) return [];
    return [...block[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
  };

  return {
    name: getString('name'),
    path: getString('path'),
    description: getString('description'),
    dependencies: getList('dependencies'),
    replacePath: getList('replace_path'),
  };
}

function parseVersionChecksum(versionText) {
  const lines = versionText.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  if (!lines.length) return ['', ''];
  if (lines.length === 1) return [lines[0], 'N/A'];
  return [lines[0], lines[1]];
}

async function findIconBlobUrl(dirHandle) {
  for (const candidate of ['icon.png', 'thumbnail.png', 'preview.png']) {
    const file = await getFileSafe(dirHandle, candidate);
    if (file) return URL.createObjectURL(file);
  }
  return null;
}

async function getDirectoryHandleSafe(parent, name) {
  try {
    return await parent.getDirectoryHandle(name);
  } catch {
    return null;
  }
}

async function getFileSafe(parent, name) {
  try {
    const handle = await parent.getFileHandle(name);
    return await handle.getFile();
  } catch {
    return null;
  }
}

async function readTextFileSafe(parent, name) {
  const file = await getFileSafe(parent, name);
  if (!file) return '';
  return file.text();
}

async function readFileHandleText(fileHandle) {
  const file = await fileHandle.getFile();
  return file.text();
}

function loadJSON(key, fallback) {
  try {
    return JSON.parse(localStorage.getItem(key) || 'null') ?? fallback;
  } catch {
    return fallback;
  }
}

function saveJSON(key, value) {
  localStorage.setItem(key, JSON.stringify(value));
}

function stripExt(filename) {
  return filename.replace(/\.[^/.]+$/, '');
}

function prettifyName(name) {
  return name.replace(/[_-]+/g, ' ').replace(/\b\w/g, (m) => m.toUpperCase());
}

function byId(id) {
  return document.getElementById(id);
}

function defaultIcon() {
  return 'data:image/svg+xml;charset=UTF-8,' + encodeURIComponent(`
    <svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">
      <rect width="64" height="64" rx="10" fill="#101828"/>
      <path d="M18 44L30 18l16 28" fill="none" stroke="#6d9dff" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
  `);
}

function escapeHtml(value) {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}
