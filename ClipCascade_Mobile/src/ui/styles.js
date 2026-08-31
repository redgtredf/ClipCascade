import { StyleSheet } from 'react-native';

// View styles shared by the App pages (extracted verbatim from App.js).
export default StyleSheet.create({
  container: {
    padding: 20,
  },
  loadingContainer: {
    flex: 1,
    justifyContent: 'center',
    alignItems: 'center',
  },
  appTitle: {
    fontSize: 28,
    fontWeight: 'bold',
    textAlign: 'center',
    paddingBottom: 20,
  },
  loadingBottomContainer: {
    position: 'absolute',
    bottom: 30,
    alignItems: 'center',
  },
  loadingText: {
    marginBottom: 10,
    fontSize: 16,
    color: '#555',
  },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    marginBottom: 10,
  },
  label: {
    flex: 1,
    fontSize: 16,
  },
  input: {
    flex: 2,
    borderWidth: 1,
    borderColor: '#ccc',
    padding: 8,
    borderRadius: 5,
  },
  loginButton: {
    backgroundColor: '#007BFF',
    padding: 10,
    borderRadius: 5,
    alignItems: 'center',
    marginVertical: 10,
  },
  loginButtonText: {
    color: 'white',
    fontSize: 16,
  },
  linkText: {
    color: '#5081ab',
    textDecorationLine: 'underline',
    textAlign: 'center',
    marginVertical: 10,
    fontSize: 18,
  },
  message: {
    color: '#5081ab',
    textAlign: 'center',
    marginVertical: 12,
    fontSize: 16,
  },
  serviceButton: {
    padding: 10,
    borderRadius: 5,
    alignItems: 'center',
    marginVertical: 10,
  },
  instructionsContainer: {
    marginTop: 40,
    paddingHorizontal: 10,
  },
  instructionsHeader: {
    fontWeight: 'bold',
    fontSize: 18,
    textAlign: 'center',
    marginBottom: 20,
  },
  instructionBlock: {
    marginTop: 15,
  },
  instructionTitle: {
    fontWeight: 'bold',
    marginBottom: 5,
    fontSize: 16,
  },
  instructionText: {
    fontSize: 14,
    lineHeight: 20,
  },
  instructionSteps: {
    marginTop: 10,
    marginLeft: 15,
  },
  footerContainer: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    marginTop: 50,
    marginBottom: 10,
    flexWrap: 'wrap',
  },
  footerText: {
    fontSize: 16,
    color: '#5081ab',
    marginTop: 5,
  },
  spacing: {
    marginHorizontal: 12,
  },
});
