ARG RUNTIME_IMAGE=eclipse-temurin:17-jre

FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /workspace
COPY settings.xml /root/.m2/settings.xml
COPY pom.xml .
RUN mvn -q -DskipTests dependency:go-offline
COPY src ./src
RUN mvn -q test
RUN mvn -q -DskipTests package

FROM ${RUNTIME_IMAGE}
WORKDIR /app
COPY --from=build /workspace/target/local-life-gateway-0.1.0-SNAPSHOT.jar app.jar
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "app.jar"]
