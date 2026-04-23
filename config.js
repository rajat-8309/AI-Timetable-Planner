// config.js
// Automatically uses localhost:5000 when developing locally,
// and the same origin (your Render domain) when deployed.
// No manual editing needed after deployment.

const API_BASE = (
  window.location.hostname === 'localhost' ||
  window.location.hostname === '127.0.0.1'
)
  ? 'http://localhost:5000/api'
  : window.location.origin + '/api';

const DEMO_MODE = false;
