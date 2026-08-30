function requireSecureServerUrl(inputUrl) {
  if (typeof inputUrl !== 'string' || inputUrl.trim() === '') {
    throw new Error('Server URL is required');
  }

  let parsed;
  try {
    parsed = new URL(inputUrl.trim());
  } catch (_error) {
    throw new Error('Server URL must be a valid https URL');
  }

  if (parsed.protocol !== 'https:') {
    throw new Error('Cleartext login is not allowed. Use an https URL.');
  }
  if (parsed.username || parsed.password) {
    throw new Error('Server URL must not contain credentials');
  }
  if (parsed.search || parsed.hash) {
    throw new Error('Server URL must not contain a query or fragment');
  }

  return parsed.href.replace(/\/+$/, '');
}

module.exports = { requireSecureServerUrl };
