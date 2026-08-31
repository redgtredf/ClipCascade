module.exports = {
  root: true,
  extends: '@react-native',
  overrides: [
    {
      // the jest setup file runs before the test framework provides globals
      files: ['jest.setup.js'],
      env: { jest: true },
    },
  ],
};
