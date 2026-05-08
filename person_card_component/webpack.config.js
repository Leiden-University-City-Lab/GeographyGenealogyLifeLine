const path = require('path');

const packagejson = require('./package.json');
const dashLibraryName = packagejson.name.replace(/-/g, '_');

module.exports = (env, argv) => {
    const mode = argv.mode || 'production';
    const entry = { main: './src/lib/index.js' };

    return {
        mode,
        entry,
        output: {
            path: path.resolve(__dirname, dashLibraryName),
            filename: `${dashLibraryName}.v${packagejson.version}.min.js`,
            library:  dashLibraryName,
            libraryTarget: 'window',
        },
        externals: {
            react:     'React',
            'react-dom': 'ReactDOM',
            'prop-types': 'PropTypes',
        },
        module: {
            rules: [
                {
                    test: /\.jsx?$/,
                    exclude: /node_modules/,
                    use: {
                        loader: 'babel-loader',
                        options: {
                            presets: [
                                ['@babel/preset-env', { targets: 'defaults' }],
                                ['@babel/preset-react', { runtime: 'classic' }],
                            ],
                        },
                    },
                },
            ],
        },
        resolve: {
            extensions: ['.js', '.jsx'],
        },
    };
};
