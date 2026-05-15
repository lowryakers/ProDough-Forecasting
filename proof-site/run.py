import os
from app import app

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    debug = os.environ.get('FLASK_ENV') != 'production'
    if debug:
        print(f'\n  ProDough Artwork Proof Site')
        print(f'  Open http://localhost:{port} in your browser\n')
    app.run(debug=debug, host='0.0.0.0', port=port)
