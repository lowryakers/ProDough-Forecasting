from app import app

if __name__ == '__main__':
    print('\n  ProDough Artwork Proof Site')
    print('  Open http://localhost:5001 in your browser\n')
    app.run(debug=True, host='0.0.0.0', port=5001)
