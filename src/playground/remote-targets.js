// Persist the selected remote model per target URL so switching between
// sleep-proxy/router endpoints does not lose the user's model choice.
const REMOTE_MODEL_STORAGE_PREFIX = 'playground.remote.selectedModel::';

function normalizeRemoteModels(rawModels) {
  if (!Array.isArray(rawModels)) {
    return [];
  }

  return rawModels
    .map((entry, index) => {
      if (typeof entry === 'string') {
        return {
          id: entry,
          label: entry,
          index,
        };
      }
      if (!entry || typeof entry !== 'object') {
        return null;
      }

      const id = entry.id || entry.model || entry.name || '';
      if (!id) {
        return null;
      }

      const node = entry.node || null;
      const nodes = Array.isArray(entry.nodes) ? entry.nodes.filter(Boolean) : [];
      const replicas = Number.isFinite(entry.replicas) ? entry.replicas : null;
      let description = '';
      if (node) {
        description = `Node: ${node}`;
      } else if (nodes.length > 0) {
        description = `Nodes: ${nodes.join(', ')}`;
      }
      if (replicas && replicas > 1) {
        description = description ? `${description} | Replicas: ${replicas}` : `Replicas: ${replicas}`;
      }

      return {
        id,
        label: id,
        description,
        node,
        nodes,
        replicas,
        index,
      };
    })
    .filter(Boolean);
}

function getActiveRemoteUrl() {
  const currentConfigUrl = window.vllmUI?.currentConfig?.remote_url;
  if (typeof currentConfigUrl === 'string' && currentConfigUrl.trim()) {
    return currentConfigUrl.trim();
  }

  const remoteUrlInput = document.getElementById('remote-url');
  if (remoteUrlInput && typeof remoteUrlInput.value === 'string' && remoteUrlInput.value.trim()) {
    return remoteUrlInput.value.trim();
  }

  return '';
}

function getStoredRemoteModel(url) {
  if (!url) {
    return '';
  }
  try {
    return window.localStorage.getItem(REMOTE_MODEL_STORAGE_PREFIX + url) || '';
  } catch (error) {
    console.warn('Failed to read stored remote model selection:', error);
    return '';
  }
}

function setStoredRemoteModel(url, modelId) {
  if (!url) {
    return;
  }
  try {
    if (modelId) {
      window.localStorage.setItem(REMOTE_MODEL_STORAGE_PREFIX + url, modelId);
    } else {
      window.localStorage.removeItem(REMOTE_MODEL_STORAGE_PREFIX + url);
    }
  } catch (error) {
    console.warn('Failed to persist remote model selection:', error);
  }
}

function getRemoteModelState() {
  if (!window.__gpuHubRemoteModelState) {
    // Keep the state on window because the upstream UI re-renders parts of the
    // settings panel and we need the selection to survive those DOM rebuilds.
    window.__gpuHubRemoteModelState = {
      models: [],
      selectedModel: '',
    };
  }
  return window.__gpuHubRemoteModelState;
}

function setSelectedRemoteModel(modelId) {
  const state = getRemoteModelState();
  state.selectedModel = modelId || '';

  const activeUrl = getActiveRemoteUrl();
  setStoredRemoteModel(activeUrl, state.selectedModel);

  if (window.vllmUI?.currentConfig && state.selectedModel) {
    window.vllmUI.currentConfig.model = state.selectedModel;
  }

  const select = document.getElementById('remote-model-preset');
  if (select && select.value !== state.selectedModel) {
    select.value = state.selectedModel;
  }

  const info = document.getElementById('remote-model-description');
  if (info) {
    const selected = state.models.find((entry) => entry.id === state.selectedModel);
    info.textContent = selected?.description || '';
  }
}

function getSelectedRemoteModel() {
  const state = getRemoteModelState();
  if (state.selectedModel) {
    return state.selectedModel;
  }

  const activeUrl = getActiveRemoteUrl();
  const stored = getStoredRemoteModel(activeUrl);
  if (stored) {
    state.selectedModel = stored;
    return stored;
  }

  return '';
}

async function waitForRemoteInputs() {
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    const group = document.getElementById('remote-settings-group');
    const urlInput = document.getElementById('remote-url');
    const keyInput = document.getElementById('remote-api-key');
    if (group && urlInput && keyInput) {
      return { group, urlInput, keyInput };
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return null;
}

function ensureRemoteModelSelector(elements) {
  const state = getRemoteModelState();
  let wrapper = document.getElementById('remote-model-selector-group');
  if (!wrapper) {
    wrapper = document.createElement('div');
    wrapper.className = 'form-group';
    wrapper.id = 'remote-model-selector-group';

    const label = document.createElement('label');
    label.setAttribute('for', 'remote-model-preset');
    label.textContent = 'Remote Model';
    wrapper.appendChild(label);

    const select = document.createElement('select');
    select.id = 'remote-model-preset';
    select.className = 'form-control';
    select.disabled = true;
    wrapper.appendChild(select);

    const help = document.createElement('small');
    help.className = 'form-help';
    help.textContent = 'Select which remote model should receive chat requests through the gateway.';
    wrapper.appendChild(help);

    const info = document.createElement('small');
    info.className = 'form-help';
    info.id = 'remote-model-description';
    wrapper.appendChild(info);

    select.addEventListener('change', () => {
      setSelectedRemoteModel(select.value);
    });

    const savedIndicator = document.getElementById('settings-saved-indicator');
    if (savedIndicator?.parentElement === elements.group) {
      elements.group.insertBefore(wrapper, savedIndicator);
    } else {
      elements.group.appendChild(wrapper);
    }
  }

  const select = wrapper.querySelector('#remote-model-preset');
  const info = wrapper.querySelector('#remote-model-description');
  if (!select || !info) {
    return null;
  }

  const models = Array.isArray(state.models) ? state.models : [];
  select.innerHTML = '';

  if (models.length === 0) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = 'Connect to a remote target to load models';
    select.appendChild(option);
    select.disabled = true;
    info.textContent = '';
    return { wrapper, select, info };
  }

  models.forEach((model) => {
    const option = document.createElement('option');
    option.value = model.id;
    option.textContent = model.label;
    select.appendChild(option);
  });

  select.disabled = false;

  let selectedModel = getSelectedRemoteModel();
  if (!selectedModel || !models.some((model) => model.id === selectedModel)) {
    selectedModel = models[0].id;
  }

  select.value = selectedModel;
  setSelectedRemoteModel(selectedModel);
  return { wrapper, select, info };
}

function renderRemoteTargetSelector(targets, elements) {
  if (!Array.isArray(targets) || targets.length === 0) {
    return;
  }
  if (document.getElementById('remote-target-preset')) {
    return;
  }

  const wrapper = document.createElement('div');
  wrapper.className = 'form-group';

  const label = document.createElement('label');
  label.setAttribute('for', 'remote-target-preset');
  label.textContent = 'Configured Remote Targets';
  wrapper.appendChild(label);

  const select = document.createElement('select');
  select.id = 'remote-target-preset';
  select.className = 'form-control';
  wrapper.appendChild(select);

  const help = document.createElement('small');
  help.className = 'form-help';
  help.textContent = 'Choose a preconfigured target. URL and API key fields are updated automatically.';
  wrapper.appendChild(help);

  const info = document.createElement('small');
  info.className = 'form-help';
  info.id = 'remote-target-description';
  wrapper.appendChild(info);

  targets.forEach((target, index) => {
    const option = document.createElement('option');
    option.value = String(index);
    option.textContent = target.name || target.url || `Target ${index + 1}`;
    select.appendChild(option);
  });

  const applyTarget = (index) => {
    const target = targets[index];
    if (!target) {
      return;
    }
    const previousUrl = (elements.urlInput.value || '').trim();
    elements.urlInput.value = target.url || '';
    elements.keyInput.value = target.apiKey || '';
    elements.urlInput.dispatchEvent(new Event('input', { bubbles: true }));
    elements.urlInput.dispatchEvent(new Event('change', { bubbles: true }));
    elements.keyInput.dispatchEvent(new Event('input', { bubbles: true }));
    elements.keyInput.dispatchEvent(new Event('change', { bubbles: true }));
    info.textContent = target.description || '';

    // Persist the chosen endpoint immediately so reloads and reconnects stay on
    // the same target even if the upstream UI has not reconnected yet.
    if (window.vllmUI && typeof window.vllmUI.saveSettings === 'function') {
      window.vllmUI.saveSettings({
        vllm_remote_url: target.url || '',
        vllm_remote_api_key: target.apiKey || '',
      });
    }

    // Switching the target invalidates the currently displayed model list.
    if (previousUrl !== (target.url || '')) {
      const state = getRemoteModelState();
      state.models = [];
      state.selectedModel = '';
      setStoredRemoteModel(previousUrl, '');
      ensureRemoteModelSelector(elements);
    }

    if (window.vllmUI && typeof window.vllmUI.updateSettingsSavedIndicator === 'function') {
      window.vllmUI.updateSettingsSavedIndicator(target.url || '');
    }
  };

  const currentUrl = (elements.urlInput.value || '').trim();
  let selectedIndex = targets.findIndex((target) => target.url === currentUrl);
  if (selectedIndex < 0) {
    selectedIndex = 0;
    if (!currentUrl) {
      applyTarget(selectedIndex);
    }
  }

  select.value = String(selectedIndex);
  info.textContent = targets[selectedIndex]?.description || '';

  select.addEventListener('change', () => {
    applyTarget(Number.parseInt(select.value, 10));
  });

  elements.group.insertBefore(wrapper, elements.group.firstChild);
}

function renderRemoteModelSelector(models, elements) {
  const state = getRemoteModelState();
  state.models = normalizeRemoteModels(models);
  ensureRemoteModelSelector(elements);
}

function patchFetchForSelectedModel() {
  if (window.__gpuHubModelFetchPatched) {
    return;
  }
  window.__gpuHubModelFetchPatched = true;

  // The upstream playground has no remote-model selector. Intercept the local
  // `/api/chat` call and inject the selected model before the backend relays it.
  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input, init = undefined) => {
    try {
      const url = typeof input === 'string' ? input : input?.url || '';
      if (url === '/api/chat' && init && typeof init.body === 'string') {
        const selectedModel = getSelectedRemoteModel();
        if (selectedModel) {
          const payload = JSON.parse(init.body);
          payload.model = selectedModel;
          init = { ...init, body: JSON.stringify(payload) };

          if (window.vllmUI?.currentConfig) {
            window.vllmUI.currentConfig.model = selectedModel;
          }
        }
      }
    } catch (error) {
      console.warn('Failed to inject selected model into /api/chat request:', error);
    }
    return originalFetch(input, init);
  };
}

function patchVllmUI() {
  if (window.__gpuHubPatchedVllmUI || !window.vllmUI) {
    return;
  }
  window.__gpuHubPatchedVllmUI = true;

  // Reuse the upstream remote server info lifecycle instead of forking the UI.
  const originalPopulateRemoteServerInfo = window.vllmUI.populateRemoteServerInfo.bind(window.vllmUI);
  window.vllmUI.populateRemoteServerInfo = (data) => {
    originalPopulateRemoteServerInfo(data);
    waitForRemoteInputs().then((elements) => {
      if (!elements) {
        return;
      }
      renderRemoteModelSelector(data?.models || [], elements);
    });
  };

  const originalHideRemoteServerInfo = window.vllmUI.hideRemoteServerInfo?.bind(window.vllmUI);
  if (originalHideRemoteServerInfo) {
    window.vllmUI.hideRemoteServerInfo = (...args) => {
      const result = originalHideRemoteServerInfo(...args);
      const state = getRemoteModelState();
      state.models = [];
      state.selectedModel = '';
      const elements = {
        group: document.getElementById('remote-settings-group'),
      };
      if (elements.group) {
        ensureRemoteModelSelector(elements);
      }
      return result;
    };
  }
}

async function initRemoteTargetPresets() {
  try {
    // Helm only injects JSON configuration; the selector logic itself ships in
    // the image so runtime pods do not need to patch application source code.
    const response = await fetch('/assets/remote-targets.json', { cache: 'no-store' });
    if (!response.ok) {
      return;
    }
    const payload = await response.json();
    const elements = await waitForRemoteInputs();
    if (!elements) {
      return;
    }
    renderRemoteTargetSelector(payload.targets || [], elements);
    ensureRemoteModelSelector(elements);
  } catch (error) {
    console.warn('Failed to initialize remote target presets:', error);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  patchFetchForSelectedModel();
  window.setTimeout(() => {
    initRemoteTargetPresets();
    patchVllmUI();
    // vllmUI is created asynchronously by the upstream bundle, so keep trying
    // until the object exists and can be wrapped.
    window.setInterval(() => {
      patchVllmUI();
    }, 500);
  }, 500);
});
