package com.example.locallife;

import org.junit.jupiter.api.Test;
import org.springframework.modulith.core.ApplicationModules;

class ModularityTests {

    @Test
    void moduleDependenciesAreAcyclicAndRespectModuleBoundaries() {
        ApplicationModules.of(LocalLifeApplication.class).verify();
    }
}
