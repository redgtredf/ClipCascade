/**
 * @format
 */

import React from 'react';
import ReactTestRenderer from 'react-test-renderer';
import App from '../App';

test('renders correctly', async () => {
  let renderer;
  await ReactTestRenderer.act(() => {
    renderer = ReactTestRenderer.create(<App />);
  });

  // Unmount and let the UI-flag poll loop observe the unmount (its sleep is
  // at most POLL_MAX_MS) so no timer outlives the test and jest can exit
  // cleanly instead of force-exiting.
  await ReactTestRenderer.act(async () => {
    renderer.unmount();
    await new Promise(resolve => setTimeout(resolve, 2100));
  });
});
