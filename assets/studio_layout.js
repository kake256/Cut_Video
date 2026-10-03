() => {
  if (document.__studioLayoutInstalled) return;
  document.__studioLayoutInstalled = true;
  const KEY = 'cut-video-studio-layout-v1';
  const defaults = {order: 'normal', search: 24, transcript: 28, height: 360};
  const number = (value, lo, hi, fallback) => {
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed >= lo && parsed <= hi ? parsed : fallback;
  };
  const load = () => { try { const raw = JSON.parse(localStorage.getItem(KEY) || '{}'); return {
    order: raw.order === 'reverse-sides' ? raw.order : defaults.order,
    search: number(raw.search, 20, 32, defaults.search), transcript: number(raw.transcript, 20, 32, defaults.transcript),
    height: number(raw.height, 300, 440, defaults.height)
  }; } catch (_) { return {...defaults}; } };
  const mount = () => {
    const root = document.getElementById('intuitive-editor-tab');
    const controls = document.getElementById('studio-layout-controls');
    if (!root || !controls || !controls.querySelector('[data-studio-reset]')) return false;
    const state = load();
    let apply = () => {
      root.style.setProperty('--studio-search-width', `${state.search}%`);
      root.style.setProperty('--studio-transcript-width', `${state.transcript}%`);
      root.style.setProperty('--studio-panel-height', `${state.height}px`);
      root.classList.toggle('studio-reverse-sides', state.order === 'reverse-sides');
      controls.querySelector('[name=order]').value = state.order;
      controls.querySelector('[name=search]').value = state.search;
      controls.querySelector('[name=transcript]').value = state.transcript;
      controls.querySelector('[name=height]').value = state.height;
    };
    const save = () => { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (_) {} apply(); };
    controls.addEventListener('input', (event) => { const target = event.target; if (!target.name) return;
      if (target.name === 'order') state.order = target.value === 'reverse-sides' ? target.value : 'normal';
      if (target.name === 'search') state.search = number(target.value, 20, 32, defaults.search);
      if (target.name === 'transcript') state.transcript = number(target.value, 20, 32, defaults.transcript);
      if (target.name === 'height') state.height = number(target.value, 300, 440, defaults.height);
      save();
    });
    controls.querySelector('[data-studio-reset]').addEventListener('click', () => { Object.assign(state, defaults); save(); });
    controls.querySelectorAll('input[type=range]').forEach((input) => {
      const output = controls.querySelector(`output[data-for="${input.name}"]`);
      if (output) output.value = input.value;
    });
    const originalApply = apply;
    apply = () => {
      originalApply();
      controls.querySelectorAll('input[type=range]').forEach((input) => {
        const output = controls.querySelector(`output[data-for="${input.name}"]`);
        if (output) output.value = input.value;
      });
    };
    apply(); controls.dataset.studioReady = 'true'; return true;
  };
  if (!mount()) { const observer = new MutationObserver(() => { if (mount()) observer.disconnect(); }); observer.observe(document.body, {childList:true, subtree:true}); }
}
