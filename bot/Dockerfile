FROM node:20

WORKDIR /app

COPY package*.json ./

RUN npm install

RUN npm audit fix --force

COPY . .

RUN npm run build

CMD ["npm", "start"]
