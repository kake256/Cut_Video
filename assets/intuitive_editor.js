() => {
  if (document.__intuitiveEditorInstalled) return;
  document.__intuitiveEditorInstalled = true;
  let drag = null;
  let commandBusy = false;
  let revisionWaitTimer = null;
  let revisionWaitToken = 0;
  let syncPollTimer = null;
  let commandSequence = 0;
  let activeRevisionWait = null;
  let playheadFrame = null;
  let pendingPlayheadAbsolute = null;
  let transcriptFocusPending = null;
  let fallbackSwitchArmed = false;
  const commandQueue = [];
  const DIRTY_EDIT_SWITCH_WARNING = '現在の編集内容はこの画面から失われます。動画を切り替えますか？';
  const PENDING_OPERATION_SWITCH_WARNING = '処理中の操作が完了した後に動画を切り替えます。よろしいですか？';
  const FALLBACK_SWITCH_KEYS = new Set(['Enter', ' ', 'ArrowDown', 'ArrowUp']);
  const AWAIT_REVISION_SLOW_MS = 15000;
  const AWAIT_REVISION_POLL_MS = 50;
  const AWAIT_REVISION_SLOW_POLL_MS = 750;
  const COMMAND_WAIT_NOTICE_ID = 'intuitive-command-wait-notice';

  const editorMeta = () => {
    const root = document.querySelector('#intuitive-toolbox [data-intuitive-root]');
    if (!root) return null;
    const meta = {
      revision: Number(root.dataset.revision), nonce: root.dataset.nonce,
      lastCommandId: root.dataset.lastCommandId || '',
      lastCommandStatus: root.dataset.lastCommandStatus || '',
      previewMode: root.dataset.previewMode || 'source',
      activeTool: root.dataset.activeTool || '',
      editDirty: root.dataset.editDirty === 'true',
      canUndo: root.dataset.canUndo === 'true',
      canRedo: root.dataset.canRedo === 'true',
      transcriptStart: Number(root.dataset.transcriptStart),
      transcriptEnd: Number(root.dataset.transcriptEnd),
      viewportStart: Number(root.dataset.viewportStart),
      viewportEnd: Number(root.dataset.viewportEnd),
      previewStart: Number(root.dataset.previewStart),
      previewEnd: Number(root.dataset.previewEnd),
      hasSelectedBoundary: root.dataset.hasSelectedBoundary === 'true',
      timelineEditMode: root.dataset.timelineEditMode === 'true'
    };
    if (transcriptFocusPending && transcriptFocusPending.nonce === meta.nonce
        && transcriptFocusPending.time >= meta.transcriptStart
        && transcriptFocusPending.time <= meta.transcriptEnd) {
      transcriptFocusPending = null;
    }
    return meta;
  };
  const intuitivePickerParts = () => {
    const picker = document.getElementById('intuitive-video-picker');
    const accordion = picker ? picker.closest('.gr-accordion') || picker : null;
    return {
      picker,
      accordion,
      content: accordion
        ? accordion.querySelector(':scope > [data-testid="accordion-content"]') : null,
      button: accordion ? accordion.querySelector(':scope > button.label-wrap') : null
    };
  };
  const openIntuitivePicker = () => {
    const {picker, accordion, content, button} = intuitivePickerParts();
    if (picker) picker.classList.add('is-intuitive-reselecting');
    if (content && button && getComputedStyle(content).display === 'none') button.click();
    if (accordion) accordion.scrollIntoView({block: 'start', behavior: 'smooth'});
  };
  const confirmDirtySessionReplacement = () => {
    const meta = editorMeta();
    const hasDirtyEdit = Boolean(meta && meta.editDirty);
    const hasPendingOperation = commandBusy || commandQueue.length > 0;
    if (!hasDirtyEdit && !hasPendingOperation) return true;
    const warning = hasDirtyEdit
      ? DIRTY_EDIT_SWITCH_WARNING : PENDING_OPERATION_SWITCH_WARNING;
    const confirmed = window.confirm(warning);
    if (confirmed) {
      // Commands that have not started belong to the session being replaced.
      // Drop them now; an in-flight command is serialized by Gradio's shared
      // concurrency_id and its eventual response precedes the replacement.
      commandQueue.length = 0;
    }
    return confirmed;
  };
  const stopDirtySessionReplacement = (event) => {
    if (confirmDirtySessionReplacement()) return false;
    event.preventDefault();
    event.stopImmediatePropagation();
    return true;
  };
  const isReplacementClick = (event) => {
    const target = event.target;
    const gallery = target.closest('#intuitive-video-gallery');
    const galleryCard = target.closest('button, [role="button"], .gallery-item');
    if (gallery && galleryCard) return true;
    return Boolean(target.closest(
      'button#intuitive-load-video, #intuitive-load-video button, ' +
      'button#intuitive-search-button, #intuitive-search-button button'
    ));
  };
  const isSearchResultSelection = (event) => {
    const resultCell = event.target.closest(
      '#intuitive-search-results .body-cell, ' +
      '#intuitive-search-results td, ' +
      '#intuitive-search-results [role="gridcell"]'
    );
    if (!resultCell) return false;
    return Boolean(resultCell.closest('.virtual-row, tr, [role="row"]'));
  };

  // All click-driven session replacements share one capture-phase guard.
  // Result-table blank space, headers and scrollbars do not match a data cell.
  document.addEventListener('click', (event) => {
    if (isReplacementClick(event)) stopDirtySessionReplacement(event);
  }, true);

  // Gradio 6.19 Dataframe dispatches selection from mousedown on a
  // .body-cell inside .virtual-row. Guard that exact phase so cancellation
  // stops the select callback; headers, blank space and scrollbars do not
  // match a data cell. Search results are excluded from the click guard above,
  // so one gesture can never prompt twice.
  document.addEventListener('mousedown', (event) => {
    if (isSearchResultSelection(event)) stopDirtySessionReplacement(event);
  }, true);

  // The visible card grid is a display-only layer. Forward its clicks (by
  // index) onto the matching button inside the hidden proxy Gallery so the
  // exact same selection path runs: the dirty-session confirm above, then
  // Gradio's own Gallery `select` handler (nonce/FIFO, auto-preview,
  // Accordion-close all stay wired to that Gallery, untouched).
  document.addEventListener('click', (event) => {
    const card = event.target.closest('#intuitive-video-card-grid .intuitive-video-card');
    if (!card) return;
    const index = Number(card.dataset.index);
    if (!Number.isInteger(index) || index < 0) return;
    const buttons = document.querySelectorAll('#intuitive-video-gallery .gallery-item button');
    const target = buttons[index];
    if (target) target.click();
  }, true);

  // The fallback Dropdown changes value before its Gradio callback.  Confirm
  // on the opening pointer/key action so cancellation cannot leave a displayed
  // value that was never loaded.  The reselect button itself does not confirm;
  // confirmation remains at the actual choice, avoiding a double prompt.
  const armFallbackSwitch = (event) => {
    if (!event.target.closest('#intuitive-video-select') || fallbackSwitchArmed) return;
    if (stopDirtySessionReplacement(event)) return;
    fallbackSwitchArmed = true;
  };
  document.addEventListener('pointerdown', armFallbackSwitch, true);
  document.addEventListener('keydown', (event) => {
    if (event.isComposing || event.keyCode === 229) return;
    // Keep fallback keyboard selection and search submit in this same capture
    // listener. Their DOM targets are mutually exclusive, so Enter can never
    // produce two confirmations for one user gesture.
    if (FALLBACK_SWITCH_KEYS.has(event.key)) armFallbackSwitch(event);
    if (
      event.key === 'Enter'
      && event.target.closest('#intuitive-search-query')
    ) stopDirtySessionReplacement(event);
    handleIntuitiveKeydown(event);
  }, true);
  document.addEventListener('focusout', (event) => {
    if (event.target.closest('#intuitive-video-select')) {
      setTimeout(() => { fallbackSwitchArmed = false; }, 0);
    }
  }, true);
  document.addEventListener('input', (event) => {
    if (event.target.closest('#intuitive-video-select')) fallbackSwitchArmed = false;
  }, true);
  const clearCommandWaitNotice = () => {
    document.getElementById(COMMAND_WAIT_NOTICE_ID)?.remove();
  };
  const showCommandWaitNotice = (message = '') => {
    const root = document.querySelector('#intuitive-toolbox [data-intuitive-root]');
    if (!root) return false;
    const existing = document.getElementById(COMMAND_WAIT_NOTICE_ID);
    const notice = existing || document.createElement('div');
    notice.id = COMMAND_WAIT_NOTICE_ID;
    notice.className = 'intuitive-command-wait-notice';
    notice.setAttribute('role', 'status');
    notice.setAttribute('aria-live', 'polite');
    notice.replaceChildren();
    const text = document.createElement('span');
    text.textContent = message || '処理中です。続けて行った操作は順番に反映されます。';
    notice.appendChild(text);
    if (!message) {
      const syncButton = document.createElement('button');
      syncButton.type = 'button';
      syncButton.textContent = '状態を再確認';
      syncButton.setAttribute('data-intuitive-sync-state', '');
      notice.appendChild(syncButton);
    }
    if (!existing) root.insertAdjacentElement('afterend', notice);
    return true;
  };
  const finishRevisionWait = (waitToken, warning = '') => {
    if (waitToken !== revisionWaitToken) return;
    if (revisionWaitTimer !== null) clearTimeout(revisionWaitTimer);
    if (syncPollTimer !== null) clearTimeout(syncPollTimer);
    revisionWaitTimer = null;
    syncPollTimer = null;
    activeRevisionWait = null;
    clearCommandWaitNotice();
    if (warning) showCommandWaitNotice(warning);
    commandBusy = false;
    flushCommandQueue();
  };
  const finishAcknowledgedCommand = (current, commandId, waitToken) => {
    if (!current || current.lastCommandId !== commandId) return false;
    if (current.lastCommandStatus === 'success') {
      finishRevisionWait(waitToken);
    } else {
      commandQueue.length = 0;
      finishRevisionWait(
        waitToken,
        '直前の操作を適用できなかったため、後続の操作を破棄しました。画面の状態を確認し、必要なら操作をやり直してください。'
      );
    }
    return true;
  };
  const newBridgeId = (prefix) => {
    const randomUUID = globalThis.crypto && globalThis.crypto.randomUUID;
    if (typeof randomUUID === 'function') return randomUUID.call(globalThis.crypto);
    commandSequence += 1;
    return `${prefix}-${Date.now()}-${commandSequence}`;
  };
  const requestStateSync = () => {
    const pending = activeRevisionWait;
    if (!pending || pending.syncToken) return;
    const field = document.querySelector(
      '#intuitive-sync-token textarea, #intuitive-sync-token input'
    );
    const button = document.querySelector(
      '#intuitive-sync-submit button, button#intuitive-sync-submit, #intuitive-sync-submit'
    );
    if (!field || !button) return;
    const syncToken = newBridgeId('sync');
    pending.syncToken = syncToken;
    const prototype = field instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, 'value').set;
    setter.call(field, syncToken);
    field.dispatchEvent(new InputEvent('input', {
      bubbles: true, composed: true, inputType: 'insertText', data: null
    }));
    requestAnimationFrame(() => button.click());
    const awaitSync = () => {
      if (!activeRevisionWait || activeRevisionWait.waitToken !== pending.waitToken) return;
      syncPollTimer = null;
      const ack = document.querySelector(
        '#intuitive-sync-ack [data-intuitive-sync-token]'
      );
      if (ack && ack.dataset.intuitiveSyncToken === syncToken) {
        const current = editorMeta();
        if (current && current.nonce !== pending.awaitedNonce) {
          finishRevisionWait(pending.waitToken);
        } else if (finishAcknowledgedCommand(
          current, pending.commandId, pending.waitToken
        )) {
          return;
        } else {
          commandQueue.length = 0;
          finishRevisionWait(
            pending.waitToken,
            '直前の操作が反映されたか確認できません。画面の状態を確認し、必要なら操作をやり直してください。後続の操作は破棄しました。'
          );
        }
        return;
      }
      syncPollTimer = setTimeout(awaitSync, 100);
    };
    if (syncPollTimer !== null) clearTimeout(syncPollTimer);
    syncPollTimer = setTimeout(awaitSync, 100);
  };
  document.addEventListener('click', (event) => {
    if (!event.target.closest('[data-intuitive-sync-state]')) return;
    event.preventDefault();
    requestStateSync();
  });
  const flushCommandQueue = () => {
    if (commandBusy || !commandQueue.length) return;
    const queued = commandQueue.shift();
    const meta = editorMeta();
    if (!meta) {
      commandQueue.unshift(queued);
      setTimeout(flushCommandQueue, 100);
      return;
    }
    if (queued.nonce !== meta.nonce) {
      flushCommandQueue();
      return;
    }
    const payload = queued.payload;
    const field = document.querySelector('#intuitive-command-json textarea, #intuitive-command-json input');
    const button = document.querySelector(
      '#intuitive-command-submit button, button#intuitive-command-submit, #intuitive-command-submit'
    );
    if (!field || !button) {
      commandQueue.unshift(queued);
      setTimeout(flushCommandQueue, 100);
      return;
    }
    commandBusy = true;
    const commandId = queued.commandId;
    const prototype = field instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, 'value').set;
    setter.call(field, JSON.stringify({...payload, ...meta}));
    field.dispatchEvent(new InputEvent('input', {
      bubbles: true, composed: true, inputType: 'insertText', data: null
    }));
    requestAnimationFrame(() => button.click());
    const started = performance.now();
    const awaitedNonce = meta.nonce;
    const waitToken = ++revisionWaitToken;
    activeRevisionWait = {waitToken, commandId, awaitedNonce, syncToken: ''};
    let slowNoticeShown = false;
    const awaitRevision = () => {
      if (waitToken !== revisionWaitToken) return;
      revisionWaitTimer = null;
      const current = editorMeta();
      const elapsed = performance.now() - started;
      if (current && current.nonce !== awaitedNonce) {
        finishRevisionWait(waitToken);
        return;
      }
      if (finishAcknowledgedCommand(current, commandId, waitToken)) return;
      if (elapsed >= AWAIT_REVISION_SLOW_MS && !slowNoticeShown) {
        slowNoticeShown = showCommandWaitNotice();
        if (slowNoticeShown) {
          console.warn(
            '[intuitive-editor] command is still waiting for its acknowledgement after',
            AWAIT_REVISION_SLOW_MS, 'ms; the queue remains paused.'
          );
        }
      }
      const pollInterval = elapsed >= AWAIT_REVISION_SLOW_MS
        ? AWAIT_REVISION_SLOW_POLL_MS : AWAIT_REVISION_POLL_MS;
      revisionWaitTimer = setTimeout(awaitRevision, pollInterval);
    };
    if (revisionWaitTimer !== null) clearTimeout(revisionWaitTimer);
    revisionWaitTimer = setTimeout(awaitRevision, AWAIT_REVISION_POLL_MS);
  };
  const currentSourcePlayhead = (meta) => {
    if (!meta || meta.previewMode === 'result') return null;
    const video = document.querySelector('#intuitive-preview-video video');
    if (!(video instanceof HTMLVideoElement)) return null;
    const currentTime = Number(video.currentTime);
    if (!Number.isFinite(currentTime) || !Number.isFinite(meta.previewStart)
        || !Number.isFinite(meta.previewEnd)) return null;
    const absolute = meta.previewStart + currentTime;
    return Math.max(meta.previewStart, Math.min(meta.previewEnd, absolute));
  };
  const send = (payload) => {
    const meta = editorMeta();
    if (!meta) return;
    const queuedPayload = {...payload};
    queuedPayload.command_id = newBridgeId('command');
    const playhead = currentSourcePlayhead(meta);
    if (Number.isFinite(playhead)) queuedPayload.playhead_sec = playhead;
    commandQueue.push({
      payload: queuedPayload, nonce: meta.nonce,
      commandId: queuedPayload.command_id
    });
    flushCommandQueue();
  };
  const currentAdjustmentStep = () => {
    const selected = document.querySelector('#intuitive-adjust-step input:checked');
    const step = Number(selected ? selected.value : 1.0);
    return Number.isFinite(step) && step > 0 ? step : 1.0;
  };
  const intuitiveEditorIsVisible = () => {
    return Array.from(document.querySelectorAll(
      '#intuitive-editor-prototype-tab'
    )).some((tab) => {
      const style = getComputedStyle(tab);
      return style.display !== 'none' && style.visibility !== 'hidden'
        && tab.getClientRects().length > 0;
    });
  };
  const handleIntuitiveKeydown = (event) => {
    if (event.defaultPrevented || event.repeat || event.isComposing || event.keyCode === 229) return;
    if (!intuitiveEditorIsVisible()) return;
    const target = event.target;
    if (!(target instanceof Element)) return;
    const editable = target.closest(
      'input, textarea, select, [contenteditable]:not([contenteditable="false"])'
    );
    const meta = editorMeta();
    const lowerKey = String(event.key || '').toLowerCase();
    if (event.ctrlKey && !event.altKey && !event.metaKey && !editable) {
      const redo = lowerKey === 'y' || (lowerKey === 'z' && event.shiftKey);
      const undo = lowerKey === 'z' && !event.shiftKey;
      if (!meta || meta.previewMode === 'result') return;
      if ((undo && meta.canUndo) || (redo && meta.canRedo)) {
        event.preventDefault();
        send({type: redo ? 'redo' : 'undo'});
      }
      return;
    }
    if (editable || event.ctrlKey || event.altKey || event.metaKey) return;

    const word = target.closest('#intuitive-transcript-words .intuitive-word[role="button"]');
    if (word) {
      if (event.key === 'Enter' || event.key === ' ' || event.key === 'Spacebar') {
        event.preventDefault();
        word.click();
        return;
      }
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault();
        const words = Array.from(document.querySelectorAll(
          '#intuitive-transcript-words .intuitive-word[role="button"]'
        ));
        const index = words.indexOf(word);
        const direction = event.key === 'ArrowLeft' ? -1 : 1;
        const next = words[Math.max(0, Math.min(words.length - 1, index + direction))];
        if (next) {
          words.forEach((item) => item.tabIndex = -1);
          next.tabIndex = 0;
          next.focus();
        }
        return;
      }
    }

    const boundary = target.closest('[data-boundary-kind][role="slider"]');
    if (boundary && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) {
      if (boundary.getAttribute('aria-disabled') === 'true' || !meta
          || meta.previewMode === 'result' || !meta.timelineEditMode) return;
      event.preventDefault();
      const direction = event.key === 'ArrowLeft' ? -1 : 1;
      const current = Number(boundary.dataset.boundaryTime);
      if (!Number.isFinite(current)) return;
      send({
        type: 'set_boundary', kind: boundary.dataset.boundaryKind,
        id: boundary.dataset.cutId || null,
        time: current + direction * currentAdjustmentStep()
      });
      return;
    }

    const viewportPart = target.closest('[data-viewport-drag][role="slider"]');
    if (viewportPart && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) {
      const overview = viewportPart.closest('[data-intuitive-overview]');
      if (!overview) return;
      event.preventDefault();
      const direction = event.key === 'ArrowLeft' ? -1 : 1;
      const delta = direction * currentAdjustmentStep();
      const duration = Number(overview.dataset.duration);
      const minSpan = Number(overview.dataset.viewportMinSpan);
      const maxSpan = Number(overview.dataset.viewportMaxSpan);
      let start = Number(overview.dataset.viewportStart);
      let end = Number(overview.dataset.viewportEnd);
      if (![duration, minSpan, maxSpan, start, end].every(Number.isFinite)) return;
      const mode = viewportPart.dataset.viewportDrag;
      if (mode === 'move') {
        const width = end - start;
        start = Math.max(0, Math.min(duration - width, start + delta));
        end = start + width;
      } else if (mode === 'start') {
        start = Math.max(0, Math.min(end - minSpan, start + delta));
        start = Math.max(start, end - maxSpan);
      } else {
        end = Math.min(duration, Math.max(start + minSpan, end + delta));
        end = Math.min(end, start + maxSpan);
      }
      send({type: 'set_viewport', start, end});
    }
  };
  const timeAtPointer = (track, event, lo, hi) => {
    const rect = track.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / Math.max(rect.width, 1)));
    return lo + ratio * (hi - lo);
  };
  const requestTranscriptFocus = (absolute) => {
    const meta = editorMeta();
    if (!meta || meta.previewMode === 'result' || !Number.isFinite(absolute)) return;
    const span = Math.max(0, meta.transcriptEnd - meta.transcriptStart);
    const margin = Math.min(8, span * .2);
    if (absolute >= meta.transcriptStart + margin && absolute <= meta.transcriptEnd - margin) {
      transcriptFocusPending = null;
      return;
    }
    const inside = absolute >= meta.transcriptStart && absolute <= meta.transcriptEnd;
    const cannotShiftLeft = meta.transcriptStart <= meta.viewportStart + .001;
    const cannotShiftRight = meta.transcriptEnd >= meta.viewportEnd - .001;
    if (inside && ((absolute < meta.transcriptStart + margin && cannotShiftLeft)
        || (absolute > meta.transcriptEnd - margin && cannotShiftRight))) {
      transcriptFocusPending = null;
      return;
    }
    if (transcriptFocusPending && transcriptFocusPending.nonce === meta.nonce
        && Math.abs(transcriptFocusPending.time - absolute) < 1) return;
    transcriptFocusPending = {nonce: meta.nonce, time: absolute};
    send({type: 'set_transcript_focus', time: absolute});
  };
  const formatTimelineTime = (value) => {
    const totalCentiseconds = Math.round(Math.max(0, Number(value) || 0) * 100);
    const hours = Math.floor(totalCentiseconds / 360000);
    const minutes = Math.floor((totalCentiseconds % 360000) / 6000);
    const remainder = ((totalCentiseconds % 6000) / 100).toFixed(2).padStart(5, '0');
    return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${remainder}`;
  };
  const updateViewportDrag = (currentDrag, event) => {
    const rect = currentDrag.track.getBoundingClientRect();
    const delta = (event.clientX - currentDrag.startX) / Math.max(rect.width, 1) * currentDrag.duration;
    const minSpan = currentDrag.minSpan;
    const maxSpan = currentDrag.maxSpan;
    let start = currentDrag.start, end = currentDrag.end;
    if (currentDrag.mode === 'move') {
      const width = end - start;
      start = Math.max(currentDrag.previewStart, Math.min(currentDrag.previewEnd - width, start + delta));
      end = start + width;
    } else if (currentDrag.mode === 'start') {
      start = Math.max(currentDrag.previewStart, Math.min(end - minSpan, start + delta));
      if (end - start > maxSpan) start = end - maxSpan;
    } else {
      end = Math.min(currentDrag.previewEnd, Math.max(start + minSpan, end + delta));
      if (end - start > maxSpan) end = start + maxSpan;
    }
    currentDrag.nextStart = start;
    currentDrag.nextEnd = end;
    currentDrag.overlay.style.left = `${start / currentDrag.duration * 100}%`;
    currentDrag.overlay.style.width = `${(end - start) / currentDrag.duration * 100}%`;
    if (currentDrag.summary) {
      currentDrag.summary.textContent = `${formatTimelineTime(start)} ～ ${formatTimelineTime(end)}（${(end - start).toFixed(1)}秒）`;
    }
  };
  const updateZoomPlayhead = (zoom, absolute) => {
    if (!zoom || zoom.dataset.previewMode === 'result' || !Number.isFinite(absolute)) return;
    const lo = Number(zoom.dataset.viewStart), hi = Number(zoom.dataset.viewEnd);
    if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi <= lo) return;
    const playhead = zoom.querySelector('.intuitive-playhead');
    if (!playhead) return;
    const clamped = Math.max(lo, Math.min(hi, absolute));
    const percent = (clamped - lo) / (hi - lo) * 100;
    playhead.style.left = `${percent}%`;
  };
  const scheduleZoomPlayhead = (absolute) => {
    if (!Number.isFinite(absolute)) return;
    pendingPlayheadAbsolute = absolute;
    if (playheadFrame !== null) return;
    playheadFrame = requestAnimationFrame(() => {
      playheadFrame = null;
      const pending = pendingPlayheadAbsolute;
      pendingPlayheadAbsolute = null;
      const currentZoom = document.querySelector('[data-intuitive-zoom]');
      updateZoomPlayhead(currentZoom, pending);
    });
  };
  const seekZoom = (zoom, event) => {
    if (!zoom || zoom.dataset.previewMode === 'result') return;
    const track = zoom.querySelector('.intuitive-zoom-track');
    const lo = Number(zoom.dataset.viewStart), hi = Number(zoom.dataset.viewEnd);
    const absolute = timeAtPointer(track, event, lo, hi);
    const video = document.querySelector('#intuitive-preview-video video');
    const previewStart = Number(zoom.dataset.previewStart), previewEnd = Number(zoom.dataset.previewEnd);
    if (video && absolute >= previewStart && absolute <= previewEnd) {
      video.currentTime = Math.max(0, absolute - previewStart);
    }
    updateZoomPlayhead(zoom, absolute);
    requestTranscriptFocus(absolute);
  };

  const syncPlayheadFromVideo = (event) => {
    const video = event.target;
    if (!(video instanceof HTMLVideoElement) || !video.closest('#intuitive-preview-video')) return;
    const zoom = document.querySelector('[data-intuitive-zoom]');
    if (!zoom || zoom.dataset.previewMode === 'result') return;
    const previewStart = Number(zoom.dataset.previewStart);
    const currentTime = Number(video.currentTime);
    if (!Number.isFinite(previewStart) || !Number.isFinite(currentTime)) return;
    const absolute = previewStart + currentTime;
    scheduleZoomPlayhead(absolute);
    if (event.type === 'timeupdate') requestTranscriptFocus(absolute);
  };
  document.addEventListener('timeupdate', syncPlayheadFromVideo, true);
  document.addEventListener('seeking', syncPlayheadFromVideo, true);

  document.addEventListener('click', (event) => {
    const meta = editorMeta();
    const pickerParts = intuitivePickerParts();
    if (pickerParts.button && pickerParts.button.contains(event.target)) {
      requestAnimationFrame(() => {
        if (pickerParts.content
            && getComputedStyle(pickerParts.content).display === 'none'
            && pickerParts.picker) {
          pickerParts.picker.classList.remove('is-intuitive-reselecting');
        }
      });
    }
    const historyButton = event.target.closest('[data-intuitive-history]');
    if (historyButton) {
      event.preventDefault();
      const action = historyButton.dataset.intuitiveHistory;
      if (!meta || meta.previewMode === 'result' || historyButton.disabled) return;
      if ((action === 'undo' && meta.canUndo) || (action === 'redo' && meta.canRedo)) {
        send({type: action});
      }
      return;
    }
    const reselectButton = event.target.closest(
      'button#intuitive-reselect-video, #intuitive-reselect-video button'
    );
    if (reselectButton) {
      event.preventDefault();
      openIntuitivePicker();
      return;
    }
    const currentButton = event.target.closest(
      'button#intuitive-apply-current, #intuitive-apply-current button'
    );
    if (currentButton) {
      event.preventDefault();
      if (!meta || meta.previewMode === 'result' || !meta.activeTool) return;
      const video = document.querySelector('#intuitive-preview-video video');
      if (!(video instanceof HTMLVideoElement) || !Number.isFinite(video.currentTime)
          || !Number.isFinite(meta.previewStart)) return;
      const absolute = meta.previewStart + Number(video.currentTime);
      if (absolute < meta.previewStart - .001 || absolute > meta.previewEnd + .001) return;
      send({type: 'set_current_position', time: absolute});
      return;
    }
    const timeButton = event.target.closest(
      'button#intuitive-apply-time, #intuitive-apply-time button'
    );
    if (timeButton) {
      event.preventDefault();
      if (!meta || meta.previewMode === 'result' || !meta.hasSelectedBoundary) return;
      const input = document.querySelector('#intuitive-selected-time input');
      const value = Number(input ? input.value : NaN);
      if (!Number.isFinite(value)) return;
      send({type: 'set_selected_time', time: value});
      return;
    }
    const removeButton = event.target.closest('[data-intuitive-remove-exclusion]');
    if (removeButton) {
      event.preventDefault();
      if (!meta || meta.previewMode === 'result' || removeButton.disabled) return;
      send({type: 'remove_exclusion', id: removeButton.dataset.intuitiveRemoveExclusion});
      return;
    }
    const clearButton = event.target.closest('[data-intuitive-clear-exclusions]');
    if (clearButton) {
      event.preventDefault();
      if (!meta || meta.previewMode === 'result' || clearButton.disabled) return;
      send({type: 'clear_exclusions'});
      return;
    }
    const adjustButton = event.target.closest(
      'button#intuitive-adjust-before, #intuitive-adjust-before button, ' +
      'button#intuitive-adjust-after, #intuitive-adjust-after button'
    );
    if (adjustButton) {
      event.preventDefault();
      if (!meta || meta.previewMode === 'result') return;
      // Sent even when no boundary is selected yet (e.g. reached via keyboard,
      // bypassing the CSS pointer-events guard below): dispatch_intuitive_command
      // rejects it server-side and handle_intuitive_command surfaces a visible
      // gr.Warning ("先に調整する境界を選択してください。"), so this never fails silently.
      // 秒数調整もHTMLツールやタイムラインと同じFIFOへ通す。
      // Gradioの独立callbackにすると、完了前のタイムライン操作が古い
      // revisionを保持したまま実行され、直前の境界編集と競合する。
      const selected = document.querySelector('#intuitive-adjust-step input:checked');
      const step = Number(selected ? selected.value : 1.0);
      const direction = adjustButton.matches('#intuitive-adjust-before')
        || adjustButton.closest('#intuitive-adjust-before') ? -1 : 1;
      send({type: 'adjust_selected', delta: direction * (Number.isFinite(step) ? step : 1.0)});
      return;
    }
    const editModeToggle = event.target.closest('[data-intuitive-toggle-edit-mode]');
    if (editModeToggle) {
      event.preventDefault();
      if (!meta || meta.previewMode === 'result') return;
      send({type: 'set_timeline_edit_mode', enabled: !meta.timelineEditMode});
      return;
    }
    const fitOverallButton = event.target.closest('[data-intuitive-fit-overall]');
    if (fitOverallButton) {
      event.preventDefault();
      if (!meta || meta.previewMode === 'result') return;
      // Applying the viewport is an explicit plan operation, but it does not
      // unlock unrelated drag gestures.
      send({type: 'fit_overall_to_viewport'});
      return;
    }
    const tool = event.target.closest('[data-intuitive-tool]');
    if (tool) {
      // Both toolboxes arm the same canonical active_tool.  Drag editing is a
      // separate lock and must not change as a side effect of tool selection.
      send({type: 'set_tool', tool: tool.dataset.intuitiveTool});
      return;
    }
    const word = event.target.closest('#intuitive-transcript-words .intuitive-word');
    if (word) {
      if (meta && meta.previewMode === 'result') return;
      document.querySelectorAll(
        '#intuitive-transcript-words .intuitive-word[role="button"]'
      ).forEach((item) => item.tabIndex = item === word ? 0 : -1);
      send({type: 'set_from_word', start: Number(word.dataset.start), end: Number(word.dataset.end)});
      return;
    }
    const cut = event.target.closest('[data-cut-id]');
    if (cut && !event.target.closest('[data-boundary-kind]')) {
      if (meta && meta.previewMode === 'result') return;
      send({type: 'select_boundary', kind: 'exclusion_start', id: cut.dataset.cutId});
      return;
    }
    const zoomTrack = event.target.closest(
      '[data-intuitive-zoom] .intuitive-zoom-track'
    );
    const zoom = zoomTrack ? zoomTrack.closest('[data-intuitive-zoom]') : null;
    if (zoom && !event.target.closest('[data-boundary-kind]')) {
      // A selected tool always turns a position click into boundary placement,
      // regardless of the independent drag-edit lock.  With no tool, click
      // keeps its ordinary seek behavior.
      if (meta && meta.previewMode !== 'result' && meta.activeTool) {
        const track = zoom.querySelector('.intuitive-zoom-track');
        const lo = Number(zoom.dataset.viewStart), hi = Number(zoom.dataset.viewEnd);
        send({type: 'set_from_timeline', time: timeAtPointer(track, event, lo, hi)});
      } else {
        seekZoom(zoom, event);
      }
    }
  });

  document.addEventListener('pointerdown', (event) => {
    if (event.target.closest('.intuitive-timeline-toolbox')) return;
    const viewportPart = event.target.closest('[data-viewport-drag]');
    if (viewportPart) {
      const root = viewportPart.closest('[data-intuitive-overview]');
      const track = root.querySelector('.intuitive-overview-track');
      drag = {
        type: 'viewport', mode: viewportPart.dataset.viewportDrag,
        root, track, startX: event.clientX,
        start: Number(root.dataset.viewportStart), end: Number(root.dataset.viewportEnd),
        previewStart: 0, previewEnd: Number(root.dataset.duration),
        duration: Number(root.dataset.duration), overlay: root.querySelector('.intuitive-overview-window'),
        minSpan: Number(root.dataset.viewportMinSpan), maxSpan: Number(root.dataset.viewportMaxSpan),
        summary: root.querySelector('[data-viewport-summary]'),
        pointerId: event.pointerId, captureTarget: viewportPart
      };
      if (viewportPart.setPointerCapture) viewportPart.setPointerCapture(event.pointerId);
      event.preventDefault();
      return;
    }
    const boundary = event.target.closest('[data-boundary-kind]');
    if (boundary) {
      const root = boundary.closest('[data-intuitive-zoom]');
      if (root && root.dataset.previewMode === 'result') return;
      // Boundary dragging is protected by the independent drag-edit lock.
      if (root && root.dataset.timelineEditMode !== 'true') return;
      drag = {
        type: 'boundary', root, track: root.querySelector('.intuitive-zoom-track'),
        kind: boundary.dataset.boundaryKind, id: boundary.dataset.cutId || null,
        element: boundary
      };
      drag.pointerId = event.pointerId; drag.captureTarget = boundary;
      if (boundary.setPointerCapture) boundary.setPointerCapture(event.pointerId);
      event.preventDefault();
      return;
    }
    const zoomTrack = event.target.closest(
      '[data-intuitive-zoom] .intuitive-zoom-track'
    );
    const zoom = zoomTrack ? zoomTrack.closest('[data-intuitive-zoom]') : null;
    if (zoom && !event.target.closest('[data-cut-id]')) {
      if (zoom.dataset.previewMode === 'result') return;
      // Empty-track cut dragging is protected by the same drag-edit lock.
      if (zoom.dataset.timelineEditMode !== 'true') return;
      const meta = editorMeta();
      // ツール選択中の短いクリックは境界指定としてclickハンドラへ渡す。
      // 未選択時だけ従来どおり横ドラッグによる途中カットを開始する。
      if (meta && meta.activeTool) return;
      const track = zoom.querySelector('.intuitive-zoom-track');
      const lo = Number(zoom.dataset.viewStart), hi = Number(zoom.dataset.viewEnd);
      const startTime = timeAtPointer(track, event, lo, hi);
      const overallStart = Number(zoom.dataset.overallStart);
      const overallEnd = Number(zoom.dataset.overallEnd);
      if (startTime < overallStart || startTime > overallEnd) return;
      const marker = document.createElement('div');
      marker.className = 'intuitive-cut-zone intuitive-new-cut';
      marker.style.left = `${(startTime - lo) / (hi - lo) * 100}%`;
      marker.style.width = '0%';
      track.appendChild(marker);
      drag = {
        type: 'new_cut', root: zoom, track, lo, hi,
        startTime, startX: event.clientX, marker, overallStart, overallEnd
      };
      drag.pointerId = event.pointerId; drag.captureTarget = track;
      if (track.setPointerCapture) track.setPointerCapture(event.pointerId);
      event.preventDefault();
    }
  });

  document.addEventListener('pointermove', (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    if (drag.type === 'boundary') {
      const lo = Number(drag.root.dataset.viewStart), hi = Number(drag.root.dataset.viewEnd);
      const time = timeAtPointer(drag.track, event, lo, hi);
      drag.time = time;
      drag.element.style.left = `${(time - lo) / (hi - lo) * 100}%`;
      return;
    }
    if (drag.type === 'new_cut') {
      const time = Math.max(
        drag.overallStart,
        Math.min(drag.overallEnd, timeAtPointer(drag.track, event, drag.lo, drag.hi))
      );
      drag.endTime = time;
      drag.distance = Math.abs(event.clientX - drag.startX);
      const left = Math.min(drag.startTime, time), right = Math.max(drag.startTime, time);
      drag.marker.style.left = `${(left - drag.lo) / (drag.hi - drag.lo) * 100}%`;
      drag.marker.style.width = `${(right - left) / (drag.hi - drag.lo) * 100}%`;
      return;
    }
    updateViewportDrag(drag, event);
  });

  const finishDrag = (event, cancelled = false) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    if (!cancelled && drag.type === 'viewport') {
      // pointerup can arrive without a final pointermove (especially on a quick
      // resize), so always derive the committed range from the release point.
      updateViewportDrag(drag, event);
    }
    if (!cancelled && drag.type === 'new_cut') {
      drag.endTime = Math.max(
        drag.overallStart,
        Math.min(drag.overallEnd, timeAtPointer(drag.track, event, drag.lo, drag.hi))
      );
      drag.distance = Math.abs(event.clientX - drag.startX);
    }
    if (!cancelled && drag.type === 'boundary' && Number.isFinite(drag.time)) {
      send({type: 'set_boundary', kind: drag.kind, id: drag.id, time: drag.time});
    } else if (!cancelled &&
      drag.type === 'new_cut' && Number.isFinite(drag.endTime)
      && drag.distance >= 5
    ) {
      send({type: 'add_exclusion', start: drag.startTime, end: drag.endTime});
    } else if (!cancelled && drag.type === 'viewport' && Number.isFinite(drag.nextStart)) {
      send({type: 'set_viewport', start: drag.nextStart, end: drag.nextEnd});
    } else if (!cancelled && drag.type === 'new_cut' && drag.distance < 5) {
      seekZoom(drag.root, event);
    }
    if (drag.marker) drag.marker.remove();
    if (drag.captureTarget && drag.captureTarget.releasePointerCapture) {
      try { drag.captureTarget.releasePointerCapture(drag.pointerId); } catch (_) {}
    }
    drag = null;
  };
  document.addEventListener('pointerup', (event) => finishDrag(event, false));
  document.addEventListener('pointercancel', (event) => finishDrag(event, true));
}