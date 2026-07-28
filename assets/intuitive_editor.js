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
  let searchMarkerFrame = null;
  let searchMarkerSourceObserver = null;
  let searchMarkerOverviewObserver = null;
  let observedSearchMarkerSource = null;
  let observedSearchMarkerOverview = null;
  let activeSearchMarkerRequestId = '';
  let blockedSearchMarkerRequestId = '';
  const rejectedSearchMarkerRequestIds = new Set();
  let selectedSearchHitId = '';
  let renderedSearchMarkerLayer = null;
  let renderedSearchMarkerSignature = '';
  let overallControlsObserver = null;
  let overallControlsFrame = null;
  let observedOverallControlsHost = null;
  const commandQueue = [];
  const DIRTY_EDIT_SWITCH_WARNING = '現在の編集内容はこの画面から失われます。動画を切り替えますか？';
  const PENDING_OPERATION_SWITCH_WARNING = '処理中の操作が完了した後に動画を切り替えます。よろしいですか？';
  const FALLBACK_SWITCH_KEYS = new Set(['Enter', ' ', 'ArrowDown', 'ArrowUp']);
  const AWAIT_REVISION_SLOW_MS = 15000;
  const AWAIT_REVISION_POLL_MS = 50;
  const AWAIT_REVISION_SLOW_POLL_MS = 750;
  const COMMAND_WAIT_NOTICE_ID = 'intuitive-command-wait-notice';

  const setEditorCommandPending = (pending) => {
    const editor = document.getElementById('intuitive-editor-tab');
    const root = document.querySelector('#intuitive-toolbox [data-intuitive-root]');
    [editor, root].forEach((element) => {
      if (!element) return;
      element.classList.toggle('is-command-pending', pending);
      if (pending) element.setAttribute('aria-busy', 'true');
      else element.removeAttribute('aria-busy');
    });
  };

  const syncOverallBoundaryControls = (meta) => {
    const selectedKind = meta ? meta.selectedBoundaryKind : '';
    const resultMode = !meta || meta.previewMode === 'result';
    document.querySelectorAll('[data-intuitive-select-overall-boundary]').forEach((button) => {
      const selected = button.dataset.intuitiveSelectOverallBoundary === selectedKind;
      button.classList.toggle('is-selected', selected);
      button.setAttribute('aria-pressed', String(selected));
      button.disabled = resultMode;
    });
    const hasOverallBoundary = selectedKind === 'overall_start'
      || selectedKind === 'overall_end';
    document.querySelectorAll('[data-intuitive-overall-adjust]').forEach((button) => {
      button.disabled = resultMode || !hasOverallBoundary;
    });
  };

  const editorMeta = () => {
    const root = document.querySelector('#intuitive-toolbox [data-intuitive-root]');
    if (!root) return null;
    let transcriptProjection = null;
    try {
      transcriptProjection = JSON.parse(root.dataset.transcriptProjection || 'null');
    } catch (_) {
      transcriptProjection = null;
    }
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
      transcriptProjection,
      viewportStart: Number(root.dataset.viewportStart),
      viewportEnd: Number(root.dataset.viewportEnd),
      previewStart: Number(root.dataset.previewStart),
      previewEnd: Number(root.dataset.previewEnd),
      hasSelectedBoundary: root.dataset.hasSelectedBoundary === 'true',
      selectedBoundaryKind: root.dataset.selectedBoundaryKind || '',
      timelineEditMode: root.dataset.timelineEditMode === 'true'
    };
    syncOverallBoundaryControls(meta);
    if (transcriptFocusPending && transcriptFocusPending.nonce === meta.nonce
        && transcriptFocusPending.time >= meta.transcriptStart
        && transcriptFocusPending.time <= meta.transcriptEnd) {
      transcriptFocusPending = null;
    }
    return meta;
  };
  const scheduleOverallControlsSync = () => {
    if (overallControlsFrame !== null) return;
    overallControlsFrame = requestAnimationFrame(() => {
      overallControlsFrame = null;
      editorMeta();
    });
  };
  const installOverallControlsObserver = (attempt = 0) => {
    const host = document.getElementById('intuitive-editor-tab');
    if (!host) {
      if (attempt < 40) {
        setTimeout(() => installOverallControlsObserver(attempt + 1), 250);
      }
      return;
    }
    if (host !== observedOverallControlsHost) {
      overallControlsObserver?.disconnect();
      overallControlsObserver = new MutationObserver(scheduleOverallControlsSync);
      overallControlsObserver.observe(host, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ['data-selected-boundary-kind', 'data-preview-mode']
      });
      observedOverallControlsHost = host;
    }
    scheduleOverallControlsSync();
  };
  const syncTranscriptDecorations = (meta) => {
    const projection = meta && meta.transcriptProjection;
    if (!projection) return;
    const overallStart = Number(projection.overall_start);
    const overallEnd = Number(projection.overall_end);
    if (!Number.isFinite(overallStart) || !Number.isFinite(overallEnd)) return;
    const exclusions = Array.isArray(projection.exclusions)
      ? projection.exclusions.map((range) => [Number(range[0]), Number(range[1])])
        .filter((range) => range.every(Number.isFinite)) : [];
    const selected = Array.isArray(projection.selected_word)
      ? projection.selected_word.map(Number) : null;
    const pending = projection.pending_cut_start === null
      ? null : Number(projection.pending_cut_start);
    const containsStart = (start, end, value) => Number.isFinite(value)
      && ((start <= value && value < end)
        || (start === end && Math.abs(value - start) < .002));
    const containsEnd = (start, end, value) => Number.isFinite(value)
      && ((start < value && value <= end)
        || (start === end && Math.abs(value - start) < .002));
    document.querySelectorAll(
      '#intuitive-transcript-words .intuitive-word[role="button"]'
    ).forEach((word) => {
      const start = Number(word.dataset.start), end = Number(word.dataset.end);
      if (!Number.isFinite(start) || !Number.isFinite(end)) return;
      word.classList.toggle('is-selected-word', Boolean(selected)
        && Math.abs(selected[0] - start) < .002
        && Math.abs(selected[1] - end) < .002);
      word.classList.toggle('is-outside-overall',
        end <= overallStart || start >= overallEnd);
      word.classList.toggle('is-excluded-word', exclusions.some(
        ([cutStart, cutEnd]) => end > cutStart && start < cutEnd
      ));
      word.classList.toggle('marks-overall-start',
        containsStart(start, end, overallStart));
      word.classList.toggle('marks-overall-end',
        containsEnd(start, end, overallEnd));
      word.classList.toggle('marks-pending-cut',
        pending !== null && containsStart(start, end, pending));
      word.classList.toggle('marks-exclusion-start', exclusions.some(
        ([cutStart]) => containsStart(start, end, cutStart)
      ));
      word.classList.toggle('marks-exclusion-end', exclusions.some(
        ([, cutEnd]) => containsEnd(start, end, cutEnd)
      ));
    });
  };
  // Local diagnostics hook used by the opt-in synthetic browser benchmark.
  // It invokes the production projection path; it does not expose editor
  // state or add another mutation route.
  globalThis.__cutVideoDiagnostics = Object.freeze({
    applyTranscriptProjection: (projection) => syncTranscriptDecorations({
      transcriptProjection: projection
    })
  });
  const searchMarkerProjection = () => {
    const source = document.querySelector(
      '#intuitive-search-marker-projection [data-intuitive-search-marker-projection]'
    );
    if (!source) return null;
    try {
      const projection = JSON.parse(source.dataset.searchMarkers || 'null');
      return projection && typeof projection === 'object' ? projection : null;
    } catch (_) {
      return null;
    }
  };
  const clearRenderedSearchMarkers = () => {
    const overview = document.querySelector('[data-intuitive-overview]');
    const layer = overview?.querySelector('[data-intuitive-search-marker-layer]');
    const legend = overview?.querySelector('[data-intuitive-search-marker-legend]');
    if (layer && layer.childElementCount) layer.replaceChildren();
    if (legend) legend.hidden = true;
    renderedSearchMarkerLayer = layer || null;
    renderedSearchMarkerSignature = '';
  };
  const blockCurrentSearchMarkers = () => {
    const current = searchMarkerProjection();
    blockedSearchMarkerRequestId = String(
      (current && current.request_id) || activeSearchMarkerRequestId || ''
    );
    if (blockedSearchMarkerRequestId) {
      rejectedSearchMarkerRequestIds.add(blockedSearchMarkerRequestId);
      while (rejectedSearchMarkerRequestIds.size > 32) {
        rejectedSearchMarkerRequestIds.delete(
          rejectedSearchMarkerRequestIds.values().next().value
        );
      }
    }
    selectedSearchHitId = '';
    clearRenderedSearchMarkers();
  };
  const syncSearchMarkers = () => {
    searchMarkerFrame = null;
    const projection = searchMarkerProjection();
    const overview = document.querySelector('[data-intuitive-overview]');
    const layer = overview?.querySelector('[data-intuitive-search-marker-layer]');
    const legend = overview?.querySelector('[data-intuitive-search-marker-legend]');
    if (!projection || !overview || !layer || !legend) {
      clearRenderedSearchMarkers();
      return;
    }
    const requestId = String(projection.request_id || '');
    if (rejectedSearchMarkerRequestIds.has(requestId)
        || (blockedSearchMarkerRequestId
          && requestId === blockedSearchMarkerRequestId)) {
      clearRenderedSearchMarkers();
      return;
    }
    if (requestId !== activeSearchMarkerRequestId) {
      activeSearchMarkerRequestId = requestId;
      selectedSearchHitId = '';
    }
    blockedSearchMarkerRequestId = '';
    syncSearchResultSelection(projection);
    const duration = Number(overview.dataset.duration);
    const videoId = overview.dataset.publicVideoId || '';
    const hits = Array.isArray(projection.hits) ? projection.hits.filter(
      (hit) => String(hit.video_id || '') === videoId
        && Number.isFinite(Number(hit.position))
        && (hit.kind === 'text' || hit.kind === 'semantic')
        && String(hit.hit_id || '')
    ) : [];
    const signature = JSON.stringify([
      requestId, videoId, duration, selectedSearchHitId,
      hits.map((hit) => [hit.hit_id, hit.kind, Number(hit.position), hit.label])
    ]);
    const existingMarkers = Array.from(
      layer.querySelectorAll(':scope > .intuitive-search-marker')
    );
    const existingDomMatches = existingMarkers.length === hits.length
      && existingMarkers.every((marker, index) => (
        marker.dataset.hitId === String(hits[index].hit_id)
        && marker.dataset.markerKind === hits[index].kind
        && marker.getAttribute('aria-current') === String(
          String(hits[index].hit_id) === selectedSearchHitId
        )
      ));
    if (renderedSearchMarkerLayer === layer
        && renderedSearchMarkerSignature === signature
        && existingDomMatches) return;
    const fragment = document.createDocumentFragment();
    hits.forEach((hit) => {
      const marker = document.createElement('button');
      const position = Math.max(0, Math.min(duration, Number(hit.position)));
      marker.type = 'button';
      marker.className = 'intuitive-search-marker';
      marker.dataset.markerKind = hit.kind;
      marker.dataset.hitId = String(hit.hit_id);
      marker.dataset.requestId = requestId;
      marker.style.left = `${duration > 0 ? position / duration * 100 : 0}%`;
      marker.title = String(hit.label || '検索ヒット');
      marker.setAttribute('aria-label', marker.title);
      marker.setAttribute(
        'aria-current', String(String(hit.hit_id) === selectedSearchHitId)
      );
      fragment.appendChild(marker);
    });
    layer.replaceChildren(fragment);
    legend.hidden = hits.length === 0;
    renderedSearchMarkerLayer = layer;
    renderedSearchMarkerSignature = signature;
  };
  const scheduleSearchMarkerSync = () => {
    if (searchMarkerFrame !== null) return;
    searchMarkerFrame = requestAnimationFrame(syncSearchMarkers);
  };
  const searchResultRows = () => Array.from(document.querySelectorAll(
    '#intuitive-search-results .virtual-row, '
    + '#intuitive-search-results tbody tr, '
    + '#intuitive-search-results [role="row"]'
  )).filter((row) => row.querySelector('.body-cell, td, [role="gridcell"]'));
  const searchResultIndex = (row) => {
    const firstCell = row?.querySelector('.body-cell, td, [role="gridcell"]');
    const match = firstCell?.textContent?.match(/[○●]\s*(\d+)/);
    return match ? Number(match[1]) - 1 : -1;
  };
  const syncSearchResultSelection = (projection) => {
    const projectedHits = Array.isArray(projection?.hits) ? projection.hits : [];
    searchResultRows().forEach((row) => {
      const index = searchResultIndex(row);
      const selected = index >= 0
        && String(projectedHits[index]?.hit_id || '') === selectedSearchHitId;
      row.setAttribute('aria-selected', String(selected));
      const firstCell = row.querySelector('.body-cell, td, [role="gridcell"]');
      if (!firstCell) return;
      const walker = document.createTreeWalker(firstCell, NodeFilter.SHOW_TEXT);
      let textNode = walker.nextNode();
      while (textNode) {
        if (/[○●]\s*\d+/.test(textNode.nodeValue || '')) {
          textNode.nodeValue = (textNode.nodeValue || '').replace(
            /[○●](\s*\d+)/, `${selected ? '●' : '○'}$1`
          );
          break;
        }
        textNode = walker.nextNode();
      }
    });
  };
  const rememberSearchResultSelection = (event) => {
    const cell = event.target.closest(
      '#intuitive-search-results .body-cell, #intuitive-search-results td, '
      + '#intuitive-search-results [role="gridcell"]'
    );
    const row = cell?.closest('.virtual-row, tr, [role="row"]');
    const host = row?.closest('#intuitive-search-results');
    if (!row || !host) return;
    const index = searchResultIndex(row);
    const projection = searchMarkerProjection();
    const hit = index >= 0 && Array.isArray(projection?.hits)
      ? projection.hits[index] : null;
    if (hit && hit.hit_id) {
      selectedSearchHitId = String(hit.hit_id);
      syncSearchResultSelection(projection);
      scheduleSearchMarkerSync();
    }
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
    if (target.closest('#intuitive-video-card-grid .intuitive-video-card')) return true;
    return Boolean(target.closest(
      'button#intuitive-load-video, #intuitive-load-video button, ' +
      '.intuitive-search-marker'
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
    if (!isReplacementClick(event)) return;
    stopDirtySessionReplacement(event);
  }, true);

  // Starting a search only updates transient SearchView. It must invalidate
  // old markers immediately, but it does not replace the current edit session
  // and therefore must not show the dirty-session warning.
  document.addEventListener('click', (event) => {
    if (event.target.closest(
      'button#intuitive-search-button, #intuitive-search-button button'
    )) blockCurrentSearchMarkers();
  }, true);

  // Gradio 6.19 Dataframe dispatches selection from mousedown on a
  // .body-cell inside .virtual-row. Guard that exact phase so cancellation
  // stops the select callback; headers, blank space and scrollbars do not
  // match a data cell. Search results are excluded from the click guard above,
  // so one gesture can never prompt twice.
  document.addEventListener('mousedown', (event) => {
    if (!isSearchResultSelection(event)) return;
    if (!stopDirtySessionReplacement(event)) rememberSearchResultSelection(event);
  }, true);

  // Send the visible card's stable video ID straight into Gradio. This avoids
  // rendering a second hidden Gallery and removes index-order coupling.
  document.addEventListener('click', (event) => {
    const card = event.target.closest('#intuitive-video-card-grid .intuitive-video-card');
    if (!card) return;
    const videoId = card.dataset.videoId || '';
    const field = document.querySelector(
      '#intuitive-video-card-command textarea, #intuitive-video-card-command input'
    );
    const submit = document.querySelector(
      '#intuitive-video-card-submit button, button#intuitive-video-card-submit, ' +
      '#intuitive-video-card-submit'
    );
    if (!videoId || !field || !submit) return;
    event.preventDefault();
    const prototype = field instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, 'value').set;
    setter.call(field, JSON.stringify({
      video_id: videoId,
      request_id: newBridgeId('video')
    }));
    field.dispatchEvent(new InputEvent('input', {
      bubbles: true, composed: true, inputType: 'insertText', data: null
    }));
    requestAnimationFrame(() => submit.click());
  }, true);

  document.addEventListener('click', (event) => {
    const marker = event.target.closest('.intuitive-search-marker');
    if (!marker || event.defaultPrevented) return;
    const field = document.querySelector(
      '#intuitive-search-marker-command textarea, '
      + '#intuitive-search-marker-command input'
    );
    const submit = document.querySelector(
      '#intuitive-search-marker-submit button, '
      + 'button#intuitive-search-marker-submit, #intuitive-search-marker-submit'
    );
    if (!field || !submit) return;
    event.preventDefault();
    selectedSearchHitId = marker.dataset.hitId || '';
    scheduleSearchMarkerSync();
    const prototype = field instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, 'value').set;
    setter.call(field, JSON.stringify({
      request_id: marker.dataset.requestId || '',
      hit_id: marker.dataset.hitId || '',
      selection_id: newBridgeId('search-hit')
    }));
    field.dispatchEvent(new InputEvent('input', {
      bubbles: true, composed: true, inputType: 'insertText', data: null
    }));
    requestAnimationFrame(() => submit.click());
  });

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
    ) blockCurrentSearchMarkers();
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
    setEditorCommandPending(false);
    flushCommandQueue();
  };
  const finishAcknowledgedCommand = (current, commandId, waitToken) => {
    if (!current || current.lastCommandId !== commandId) return false;
    if (current.lastCommandStatus === 'success') {
      syncTranscriptDecorations(current);
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
    // Feedback starts when the command leaves the browser, not after Gradio
    // finishes its round-trip. The 15-second notice below remains the slower
    // recovery affordance for an unusually long acknowledgement.
    setEditorCommandPending(true);
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
      '#intuitive-editor-tab'
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
    if (boundary && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
      if (boundary.getAttribute('aria-disabled') === 'true' || !meta
          || meta.previewMode === 'result' || !meta.timelineEditMode) return;
      event.preventDefault();
      const current = Number(boundary.dataset.boundaryTime);
      if (!Number.isFinite(current)) return;
      let time;
      if (event.key === 'Home' || event.key === 'End') {
        time = Number(boundary.getAttribute(
          event.key === 'Home' ? 'aria-valuemin' : 'aria-valuemax'
        ));
      } else {
        const direction = event.key === 'ArrowLeft' ? -1 : 1;
        time = current + direction * currentAdjustmentStep();
      }
      if (!Number.isFinite(time)) return;
      send({
        type: 'set_boundary', kind: boundary.dataset.boundaryKind,
        id: boundary.dataset.cutId || null,
        time
      });
      return;
    }

    const viewportPart = target.closest('[data-viewport-drag][role="slider"]');
    if (viewportPart && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
      const overview = viewportPart.closest('[data-intuitive-overview]');
      if (!overview) return;
      event.preventDefault();
      const duration = Number(overview.dataset.duration);
      const minSpan = Number(overview.dataset.viewportMinSpan);
      const maxSpan = Number(overview.dataset.viewportMaxSpan);
      let start = Number(overview.dataset.viewportStart);
      let end = Number(overview.dataset.viewportEnd);
      if (![duration, minSpan, maxSpan, start, end].every(Number.isFinite)) return;
      const mode = viewportPart.dataset.viewportDrag;
      if (event.key === 'Home' || event.key === 'End') {
        const targetValue = Number(viewportPart.getAttribute(
          event.key === 'Home' ? 'aria-valuemin' : 'aria-valuemax'
        ));
        if (!Number.isFinite(targetValue)) return;
        if (mode === 'move') {
          const width = end - start;
          start = targetValue;
          end = start + width;
        } else if (mode === 'start') {
          start = targetValue;
        } else {
          end = targetValue;
        }
      } else if (mode === 'move') {
        const direction = event.key === 'ArrowLeft' ? -1 : 1;
        const delta = direction * currentAdjustmentStep();
        const width = end - start;
        start = Math.max(0, Math.min(duration - width, start + delta));
        end = start + width;
      } else if (mode === 'start') {
        const direction = event.key === 'ArrowLeft' ? -1 : 1;
        const delta = direction * currentAdjustmentStep();
        start = Math.max(0, Math.min(end - minSpan, start + delta));
        start = Math.max(start, end - maxSpan);
      } else {
        const direction = event.key === 'ArrowLeft' ? -1 : 1;
        const delta = direction * currentAdjustmentStep();
        end = Math.min(duration, Math.max(start + minSpan, end + delta));
        end = Math.min(end, start + maxSpan);
      }
      send({type: 'set_viewport', start, end});
    }
  };
  const timeAtPointer = (track, event, lo, hi, grabOffsetX = 0) => {
    const rect = track.getBoundingClientRect();
    const pointerX = event.clientX - grabOffsetX;
    const ratio = Math.max(0, Math.min(1, (pointerX - rect.left) / Math.max(rect.width, 1)));
    return lo + ratio * (hi - lo);
  };

  const beginDragFeedback = (currentDrag) => {
    if (!currentDrag) return;
    currentDrag.root?.classList.add('is-intuitive-dragging');
    currentDrag.root?.setAttribute('data-intuitive-dragging', currentDrag.type);
    currentDrag.captureTarget?.classList.add('is-intuitive-dragging');
  };
  const clearDragFeedback = (currentDrag) => {
    if (!currentDrag) return;
    currentDrag.root?.classList.remove('is-intuitive-dragging');
    currentDrag.root?.removeAttribute('data-intuitive-dragging');
    currentDrag.captureTarget?.classList.remove('is-intuitive-dragging');
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
    const overallBoundaryButton = event.target.closest(
      '[data-intuitive-select-overall-boundary]'
    );
    if (overallBoundaryButton) {
      event.preventDefault();
      if (!meta || meta.previewMode === 'result' || overallBoundaryButton.disabled) return;
      const kind = overallBoundaryButton.dataset.intuitiveSelectOverallBoundary;
      if (kind !== 'overall_start' && kind !== 'overall_end') return;
      // Give immediate local selection feedback. The mutation itself still
      // goes through the canonical FIFO command bridge.
      syncOverallBoundaryControls({...meta, selectedBoundaryKind: kind});
      send({type: 'select_boundary', kind});
      return;
    }
    const overallAdjustButton = event.target.closest('[data-intuitive-overall-adjust]');
    if (overallAdjustButton) {
      event.preventDefault();
      if (!meta || meta.previewMode === 'result' || overallAdjustButton.disabled) return;
      const selected = document.querySelector('[data-intuitive-overall-step]:checked');
      const step = Number(selected ? selected.value : 1.0);
      const direction = Number(overallAdjustButton.dataset.intuitiveOverallAdjust);
      if (!Number.isFinite(step) || step <= 0 || (direction !== -1 && direction !== 1)) return;
      send({type: 'adjust_selected', delta: direction * step});
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
      beginDragFeedback(drag);
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
        element: boundary,
        // Keep the exact point where the handle was grabbed under the pointer.
        // Precision boundaries intentionally have no momentum or bounce.
        grabOffsetX: event.clientX
          - (boundary.getBoundingClientRect().left + boundary.getBoundingClientRect().width / 2)
      };
      drag.pointerId = event.pointerId; drag.captureTarget = boundary;
      if (boundary.setPointerCapture) boundary.setPointerCapture(event.pointerId);
      beginDragFeedback(drag);
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
      beginDragFeedback(drag);
      event.preventDefault();
    }
  });

  document.addEventListener('pointermove', (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    if (drag.type === 'boundary') {
      const lo = Number(drag.root.dataset.viewStart), hi = Number(drag.root.dataset.viewEnd);
      const time = timeAtPointer(drag.track, event, lo, hi, drag.grabOffsetX);
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
    const finishedDrag = drag;
    if (!cancelled && finishedDrag.type === 'viewport') {
      // pointerup can arrive without a final pointermove (especially on a quick
      // resize), so always derive the committed range from the release point.
      updateViewportDrag(finishedDrag, event);
    }
    if (!cancelled && finishedDrag.type === 'new_cut') {
      finishedDrag.endTime = Math.max(
        finishedDrag.overallStart,
        Math.min(finishedDrag.overallEnd, timeAtPointer(
          finishedDrag.track, event, finishedDrag.lo, finishedDrag.hi
        ))
      );
      finishedDrag.distance = Math.abs(event.clientX - finishedDrag.startX);
    }
    if (!cancelled && finishedDrag.type === 'boundary' && Number.isFinite(finishedDrag.time)) {
      send({
        type: 'set_boundary', kind: finishedDrag.kind,
        id: finishedDrag.id, time: finishedDrag.time
      });
    } else if (!cancelled &&
      finishedDrag.type === 'new_cut' && Number.isFinite(finishedDrag.endTime)
      && finishedDrag.distance >= 5
    ) {
      send({
        type: 'add_exclusion', start: finishedDrag.startTime,
        end: finishedDrag.endTime
      });
    } else if (!cancelled && finishedDrag.type === 'viewport'
      && Number.isFinite(finishedDrag.nextStart)) {
      send({
        type: 'set_viewport', start: finishedDrag.nextStart,
        end: finishedDrag.nextEnd
      });
    } else if (!cancelled && finishedDrag.type === 'new_cut'
      && finishedDrag.distance < 5) {
      seekZoom(finishedDrag.root, event);
    }
    if (finishedDrag.marker) finishedDrag.marker.remove();
    clearDragFeedback(finishedDrag);
    // Clear first: releasePointerCapture may synchronously dispatch
    // lostpointercapture in some engines.
    drag = null;
    if (finishedDrag.captureTarget && finishedDrag.captureTarget.releasePointerCapture) {
      try {
        finishedDrag.captureTarget.releasePointerCapture(finishedDrag.pointerId);
      } catch (_) {}
    }
  };
  document.addEventListener('pointerup', (event) => finishDrag(event, false));
  document.addEventListener('pointercancel', (event) => finishDrag(event, true));
  document.addEventListener('lostpointercapture', (event) => finishDrag(event, true));

  const installSearchMarkerObservers = (attempt = 0) => {
    const source = document.getElementById('intuitive-search-marker-projection');
    // Observe the stable editor-tab boundary rather than either gr.HTML
    // wrapper: Gradio may replace the timeline Tabs subtree when a selected
    // hit opens a new source document. This remains scoped to one editor tab
    // (not the whole document) and rAF/signature checks coalesce its updates.
    const overview = document.getElementById('intuitive-editor-tab')
      || document.getElementById('intuitive-timeline-tabs')
      || document.getElementById('intuitive-overview-timeline');
    if (!source || !overview) {
      if (attempt < 40) setTimeout(() => installSearchMarkerObservers(attempt + 1), 250);
      return;
    }
    if (source !== observedSearchMarkerSource) {
      searchMarkerSourceObserver?.disconnect();
      searchMarkerSourceObserver = new MutationObserver(scheduleSearchMarkerSync);
      searchMarkerSourceObserver.observe(source, {
        childList: true, subtree: true, attributes: true
      });
      observedSearchMarkerSource = source;
    }
    if (overview !== observedSearchMarkerOverview) {
      searchMarkerOverviewObserver?.disconnect();
      searchMarkerOverviewObserver = new MutationObserver(scheduleSearchMarkerSync);
      searchMarkerOverviewObserver.observe(overview, {childList: true, subtree: true});
      observedSearchMarkerOverview = overview;
    }
    scheduleSearchMarkerSync();
  };
  document.addEventListener('cut-video:sync-search-markers', () => {
    // A Gradio output may replace either HTML component wrapper. Re-discover
    // the narrowly scoped hosts after the existing open-result callback has
    // committed, then project the still-current SearchView into the new DOM.
    installSearchMarkerObservers();
    scheduleSearchMarkerSync();
  });
  installSearchMarkerObservers();
  installOverallControlsObserver();
}
