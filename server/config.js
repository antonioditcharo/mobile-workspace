/**
 * Configuration. Reads .env (no dependency — the format is simple enough)
 * with process.env taking precedence, so container/CI env vars win.
 */

const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');

function parseEnvFile(file) {
  if (!fs.existsSync(file)) return {};
  const out = {};
  for (const raw of fs.readFileSync(file, 'utf8').split('\n')) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq === -1) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    out[key] = value;
  }
  return out;
}

function loadConfig() {
  const fileEnv = parseEnvFile(path.join(ROOT, '.env'));
  const env = { ...fileEnv, ...process.env };

  return {
    port: Number(env.PORT) || 3000,
    host: env.HOST || '0.0.0.0',
    provider: env.PROVIDER || 'hf',
    hfToken: env.HF_TOKEN || env.HUGGINGFACE_API_KEY || '',
    // "auto" looks up which partner provider serves the model. Set a name
    // (fal-ai, replicate, novita, hf-inference) to force one.
    hfProvider: env.HF_PROVIDER || 'auto',
    defaultModel: env.DEFAULT_MODEL || 'Wan-AI/Wan2.2-T2V-A14B',

    customEndpoint: env.CUSTOM_ENDPOINT || '',
    customAuthHeader: env.CUSTOM_AUTH_HEADER || 'Authorization',
    customAuthValue: env.CUSTOM_AUTH_VALUE || '',
    customBodyTemplate: env.CUSTOM_BODY_TEMPLATE || '',

    outputDir: path.resolve(ROOT, env.OUTPUT_DIR || 'outputs'),
    maxConcurrent: Number(env.MAX_CONCURRENT) || 1,
    jobTimeoutMs: Number(env.JOB_TIMEOUT_MS) || 15 * 60 * 1000,
    retainJobs: Number(env.RETAIN_JOBS) || 100,
  };
}

module.exports = { loadConfig, parseEnvFile, ROOT };
