package main

import "net/http"

// handleHealth is the HTTP request handler for the readiness probe.
func handleHealth(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("ready"))
}

func main() {
	http.HandleFunc("/health", handleHealth)
	_ = http.ListenAndServe(":8080", nil)
}
