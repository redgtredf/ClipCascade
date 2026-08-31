/**
 * Render tests for the extracted UI pages (src/ui/*): each page renders
 * with representative props without throwing, and its key content is
 * present. These catch prop-threading mistakes in the decomposition.
 */

import React from 'react';
import { Text } from 'react-native';
import ReactTestRenderer from 'react-test-renderer';

import Footer from '../ui/Footer';
import InstructionsSection from '../ui/InstructionsSection';
import LoadingPage from '../ui/LoadingPage';
import LoginPage from '../ui/LoginPage';
import WebSocketPage from '../ui/WebSocketPage';

function collectStrings(value) {
  if (value == null || typeof value === 'boolean') {
    return [];
  }
  if (typeof value === 'string' || typeof value === 'number') {
    return [String(value)];
  }
  if (Array.isArray(value)) {
    return value.flatMap(collectStrings);
  }
  return [];
}

function renderTexts(component, props) {
  let renderer;
  let text = '';
  // first act: create and flush the concurrent mount
  ReactTestRenderer.act(() => {
    renderer = ReactTestRenderer.create(
      React.createElement(component, props),
    );
  });
  // second act: read the committed tree, then unmount
  ReactTestRenderer.act(() => {
    text = renderer.root
      .findAllByType(Text)
      .map(node => collectStrings(node.props.children).join(''))
      .join('\n');
    renderer.unmount();
  });
  return text;
}


test('LoadingPage shows the app title and message', () => {
  const text = renderTexts(LoadingPage, { message: 'Verifying Session...' });
  expect(text).toContain('ClipCascade');
  expect(text).toContain('Verifying Session...');
});


test('Footer renders donate and homepage links when provided', () => {
  const withAll = renderTexts(Footer, {
    donateUrl: 'https://donate.example',
    homepageUrl: 'https://home.example',
  });
  expect(withAll).toContain('GITHUB');
  expect(withAll).toContain('HELP');
  expect(withAll).toContain('DONATE');
  expect(withAll).toContain('HOMEPAGE');

  const bare = renderTexts(Footer, {});
  expect(bare).toContain('GITHUB');
  expect(bare).not.toContain('DONATE');
  expect(bare).not.toContain('HOMEPAGE');
});


test('InstructionsSection renders the ADB setup commands', () => {
  const text = renderTexts(InstructionsSection, {});
  expect(text).toContain('Instructions');
  expect(text).toContain('adb -d shell pm grant com.clipcascade android.permission.READ_LOGS');
  expect(text).toContain('Battery Optimization Settings');
});


describe('LoginPage', () => {
  const baseProps = {
    data: {
      username: 'user@example.com',
      server_url: 'https://example.com',
      cipher_enabled: 'true',
      hash_rounds: '664937',
      salt: '',
      save_password: 'false',
      max_clipboard_size_local_limit_bytes: '',
      relaunch_on_boot: 'false',
      enable_websocket_status_notification: 'false',
      enable_periodic_checks: 'true',
      enable_image_sharing: 'true',
      enable_file_sharing: 'true',
    },
    password: 'secret',
    onPasswordChange: () => {},
    onFieldChange: () => {},
    onLogin: () => {},
    loginStatusMessage: '',
    showExtraConfig: false,
    onToggleExtraConfig: () => {},
    donateUrl: null,
  };

  test('renders the core form and hides extra config by default', () => {
    const text = renderTexts(LoginPage, baseProps);
    expect(text).toContain('Username:');
    expect(text).toContain('Password:');
    expect(text).toContain('Server URL:');
    expect(text).toContain('Login');
    expect(text).toContain('Enable Extra Config');
    expect(text).not.toContain('Hash Rounds:');
  });

  test('shows the extra config fields when toggled on', () => {
    const text = renderTexts(LoginPage, {
      ...baseProps,
      showExtraConfig: true,
    });
    expect(text).toContain('Hash Rounds:');
    expect(text).toContain('Salt:');
    expect(text).toContain('Enable Image Sharing:');
    expect(text).toContain('Hide Extra Config');
  });

  test('shows the login status message', () => {
    const text = renderTexts(LoginPage, {
      ...baseProps,
      loginStatusMessage: '❌ Login failed: 403',
    });
    expect(text).toContain('❌ Login failed: 403');
  });
});


describe('WebSocketPage', () => {
  const baseProps = {
    wsIsRunning: 'false',
    onToggleService: () => {},
    onLogout: () => {},
    wsPageMessage: '✅ Connected',
    wsPageP2PMessage: '',
    enableFilesDownloadButton: false,
    onDownloadFiles: () => {},
    newVersionAvailable: [false, ''],
    donateUrl: null,
    serverUrl: 'https://example.com',
  };

  test('stopped state shows Start, status message and instructions', () => {
    const text = renderTexts(WebSocketPage, baseProps);
    expect(text).toContain('Start');
    expect(text).toContain('Logout');
    expect(text).toContain('✅ Connected');
    expect(text).not.toContain('📥 Download File(s)');
    expect(text).toContain('Instructions');
  });

  test('running state shows Stop and the download action when armed', () => {
    const text = renderTexts(WebSocketPage, {
      ...baseProps,
      wsIsRunning: 'true',
      enableFilesDownloadButton: true,
    });
    expect(text).toContain('Stop');
    expect(text).toContain('📥 Download File(s)');
  });

  test('shows the update banner when a new version is available', () => {
    const text = renderTexts(WebSocketPage, {
      ...baseProps,
      newVersionAvailable: [true, '9.9.9'],
    });
    expect(text).toContain('New version available!');
    expect(text).toContain('9.9.9');
  });
});
