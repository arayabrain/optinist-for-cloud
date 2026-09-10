module.exports = function override(config) {
  // CRA interpolates this into a JS string literal in index.html, where the runtime guard is already too late to contain a value that breaks out.
  const gtmId = process.env.REACT_APP_GTM_ID
  if (gtmId && !/^GTM-[A-Z0-9]+$/.test(gtmId)) {
    throw new Error(
      `Malformed REACT_APP_GTM_ID: '${gtmId}'. Expected GTM-XXXXXXX.`,
    )
  }

  const okv = {
    fallback: {
      stream: require.resolve("stream-browserify"),
    },
  }
  if (!config.resolve) {
    config.resolve = okv
  } else {
    config.resolve.fallback = { ...config.resolve.fallback, ...okv.fallback }
  }
  return config
}
