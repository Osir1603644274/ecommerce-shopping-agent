FROM eclipse-temurin:17-jre
WORKDIR /app
COPY target/inventory-service-0.1.0-SNAPSHOT.jar app.jar
EXPOSE 8083
ENTRYPOINT ["java","-jar","/app/app.jar"]
