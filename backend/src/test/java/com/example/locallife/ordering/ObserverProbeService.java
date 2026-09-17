package com.example.locallife.ordering;

/** No bean annotation: isolated AOP fixture, never part of the application. */
public class ObserverProbeService {
    public int count(String privateArgument) { return 7; }
    public void fail() { throw new IllegalStateException("private exception detail"); }
}
